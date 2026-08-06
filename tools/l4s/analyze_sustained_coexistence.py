#!/usr/bin/env python3
"""Analyze one-second MoQ/TCP coexistence timelines."""

import argparse
import csv
import json
import re
import statistics
import subprocess
from pathlib import Path

MODES = {"l4s-off": "Reno / Not-ECT", "l4s-ect0": "Reno / ECT(0)",
         "l4s-on": "Prague / ECT(1)"}
IP_ECN_CODES = {"ect0": 2, "ect1": 1, "ce": 3}


def overlap_bins(metadata, count=60):
    start = metadata["foreground_started_epoch"] - metadata["tcp_started_epoch"]
    return [start + i for i in range(count)]


def resample(samples, count=60):
    bins = [[] for _ in range(count)]
    for sample in samples:
        index = int(float(sample["time_us"]) / 1_000_000)
        if 0 <= index < count:
            bins[index].append(sample)
    return [group[-1] if group else None for group in bins]


def aligned_interval_index(tcp_start, foreground_start, interval_start):
    return int(tcp_start + float(interval_start) - foreground_start)


def iperf_intervals(path, tcp_start, foreground_start, count=60):
    data = json.loads(Path(path).read_text())
    result = [None] * count
    for interval in data.get("intervals", []):
        streams = interval.get("streams", [])
        if not streams or "snd_cwnd" not in streams[0]:
            raise ValueError(f"{path}: missing snd_cwnd interval")
        stream = streams[0]
        relative = float(interval.get("sum", {}).get("start", 0))
        index = aligned_interval_index(tcp_start, foreground_start, relative)
        if 0 <= index < count:
            result[index] = stream
    return result


def packet_bins(path, port, destination, start_epoch, count=60):
    result = subprocess.run(
        ["tshark", "-r", str(path), "-Y",
         f"ip.dst == {destination} && (tcp.port == {port} or udp.port == {port})",
         "-T", "fields",
         "-e", "frame.time_epoch", "-e", "ip.len"],
        check=False, text=True, capture_output=True)
    if result.returncode not in (0, 2):
        raise ValueError(f"{path}: tshark failed: {result.stderr.strip()}")
    bins = [0] * count
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or not fields[1]:
            continue
        index = int(float(fields[0]) - start_epoch)
        if 0 <= index < count:
            bins[index] += int(fields[1])
    return [value * 8 / 1e6 for value in bins]


def ecn_counts(path, destination):
    result = subprocess.run(
        ["tshark", "-r", str(path), "-Y",
         f"ip.dst == {destination} && udp.port == 4443", "-T", "fields",
         "-e", "ip.dsfield.ecn"],
        check=False, text=True, capture_output=True)
    if result.returncode not in (0, 2):
        raise ValueError(f"{path}: tshark failed: {result.stderr.strip()}")
    counts = {0: 0, 1: 0, 2: 0, 3: 0}
    for value in result.stdout.splitlines():
        if value:
            counts[int(value, 0)] = counts.get(int(value, 0), 0) + 1
    return counts


def l4s_qdisc_packets(path):
    text = Path(path).read_text(encoding="utf-8")
    snapshots = re.split(r"^snapshot=", text, flags=re.MULTILINE)
    if len(snapshots) < 3:
        raise ValueError(f"{path}: missing before/after qdisc snapshots")
    before = sum(int(value) for value in re.findall(
        r"pkts_in_l\s+(\d+)", snapshots[1]))
    after = sum(int(value) for value in re.findall(
        r"pkts_in_l\s+(\d+)", snapshots[-1]))
    return max(0, after - before)


def mode_checks(mode, rows):
    ect0 = max(row["ect0_packets"] for row in rows)
    ect1 = max(row["ect1_packets"] for row in rows)
    ce = max(row["ce_packets"] for row in rows)
    alpha = {row["alpha_numerator"] for row in rows}
    if mode == "l4s-off" and (ect0 or ect1 or ce):
        raise ValueError("Not-ECT mode has ECN evidence")
    if mode == "l4s-ect0" and (not ect0 or not ce or ect1):
        raise ValueError("ECT(0) mode invariants failed")
    if mode == "l4s-on" and (not ect1 or not ce or len(alpha) < 2):
        raise ValueError("Prague mode invariants failed")
    if mode == "l4s-on":
        for before, after in zip(rows, rows[1:]):
            if after["ce_packets"] > before["ce_packets"] and after["cwnd_bytes"] < before["cwnd_bytes"]:
                break
        else:
            raise ValueError("Prague mode has no CE-associated cwnd reduction")


def analyze_case(case):
    issues = []
    metadata = json.loads((case / "metadata.json").read_text())
    with (case / "metrics.csv").open(newline="") as stream:
        samples = list(csv.DictReader(stream))
    if len(samples) < 5:
        raise ValueError(f"{case}: insufficient foreground metrics")
    for sample in samples:
        for field in ("time_us", "rtt_us", "cwnd_bytes", "ect1_packets",
                      "ce_packets", "alpha_numerator"):
            sample[field] = int(sample[field])
        sample["ect0_packets"] = int(sample.get("ect0_packets", 0))
    duration = metadata["duration_seconds"]
    foreground = resample(samples, duration)
    if sum(row is not None for row in foreground) < duration * .95:
        raise ValueError(f"{case}: foreground metrics cover less than 95% of overlap")
    if foreground[0] is None or foreground[-1] is None:
        raise ValueError(f"{case}: foreground metrics do not span the full overlap")
    previous = foreground[0]
    for index, row in enumerate(foreground):
        if row is None:
            foreground[index] = previous
        else:
            previous = row
    try:
        mode_checks(metadata["mode"], foreground)
    except ValueError as error:
        issues.append(str(error))
    tcp = iperf_intervals(case / "iperf-client.json",
                          metadata["tcp_started_epoch"],
                          metadata["foreground_started_epoch"],
                          metadata["duration_seconds"])
    present = [row for row in tcp if row is not None]
    if len(present) < metadata["duration_seconds"] * .9:
        issues.append("fewer than 90% TCP overlap bins")
    captures = sorted(case.glob("*.pcap"))
    if not captures or any(path.stat().st_size == 0 for path in captures):
        raise ValueError(f"{case}: missing or empty packet capture")
    foreground_wire = [0.0] * duration
    background_wire = [0.0] * duration
    captures = [path for path in captures if path.name == "switch-client.pcap"]
    if not captures:
        raise ValueError(f"{case}: missing client-side forward capture")
    for capture in captures:
        foreground_values = packet_bins(
            capture, 4443, metadata["client_ip"],
            metadata["foreground_started_epoch"], duration)
        background_values = packet_bins(
            capture, 5201, metadata["client_ip"],
            metadata["foreground_started_epoch"], duration)
        foreground_wire = [a + b for a, b in zip(foreground_wire, foreground_values)]
        background_wire = [a + b for a, b in zip(background_wire, background_values)]
    ecn = {key: 0 for key in ("ect0", "ect1", "ce")}
    for capture in captures:
        counts = ecn_counts(capture, metadata["client_ip"])
        for signal, code in IP_ECN_CODES.items():
            ecn[signal] += counts.get(code, 0)
    if metadata["mode"] == "l4s-off" and any(ecn.values()):
        issues.append("Not-ECT capture contains ECN-marked packets")
    if metadata["mode"] == "l4s-ect0" and (not ecn["ect0"] or not ecn["ce"] or ecn["ect1"]):
        issues.append("ECT(0) capture invariant failed")
    if metadata["mode"] == "l4s-on" and (not ecn["ect1"] or not ecn["ce"] or ecn["ect0"]):
        issues.append("Prague capture invariant failed")
    qdisc_packets = l4s_qdisc_packets(case / "dualpi2-stats.txt")
    if metadata["mode"] == "l4s-on" and qdisc_packets == 0:
        issues.append("no L4S DualPI2 classification evidence")
    tcp_rate = statistics.mean(background_wire)
    active_tcp_bins = sum(rate > 0 for rate in background_wire)
    combined_rate = statistics.mean(
        foreground_wire[i] + background_wire[i] for i in range(duration))
    if not 14.0 <= combined_rate <= 21.0:
        issues.append(f"combined rate is {combined_rate:.2f} Mbit/s")
    return metadata, foreground, tcp, foreground_wire, background_wire, issues


def write_timeline(case, metadata, foreground, tcp, foreground_wire, background_wire):
    path = case / "timeline.csv"
    fields = ("time_s", "foreground_cwnd_bytes", "foreground_rtt_us",
              "foreground_ect0_packets", "foreground_ect1_packets",
              "foreground_ce_packets", "prague_alpha", "foreground_wire_mbps",
              "tcp_wire_mbps", "combined_wire_mbps",
              "bottleneck_utilization_percent", "foreground_share",
              "tcp_cwnd_bytes")
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index, row in enumerate(foreground):
            tcp_row = tcp[index] if index < len(tcp) else None
            foreground_rate = foreground_wire[index]
            tcp_rate = background_wire[index]
            writer.writerow({
                "time_s": index,
                "foreground_cwnd_bytes": row["cwnd_bytes"],
                "foreground_rtt_us": row["rtt_us"],
                "foreground_ect0_packets": row.get("ect0_packets", 0),
                "foreground_ect1_packets": row["ect1_packets"],
                "foreground_ce_packets": row["ce_packets"],
                "prague_alpha": row["alpha_numerator"],
                "foreground_wire_mbps": foreground_rate,
                "tcp_wire_mbps": tcp_rate,
                "combined_wire_mbps": foreground_rate + tcp_rate,
                "bottleneck_utilization_percent":
                    (foreground_rate + tcp_rate) / 20 * 100,
                "foreground_share": foreground_rate /
                    max(foreground_rate + tcp_rate, 1e-9),
                "tcp_cwnd_bytes": (tcp_row or {}).get("snd_cwnd", 0),
            })


PLOT_COLORS = {"foreground": "#c44e52", "tcp": "#0072b2", "alpha": "#e69f00",
               "ect0": "#9467bd", "ect1": "#2ca02c", "utilisation": "#4c78a8"}


def svg_polyline(values, x, y, width, height, scale, color):
    points = " ".join(
        f"{x + index * width / max(len(values) - 1, 1):.1f},"
        f"{y + height - value / scale * height:.1f}"
        for index, value in enumerate(values))
    return (f'<polyline fill="none" stroke="{color}" stroke-width="1.8" '
            f'points="{points}"/>')


def render_svg(case, mode):
    with (case / "timeline.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    width, header, panel_height, footer = 1180, 58, 166, 24
    height = header + panel_height * 4 + footer
    plot_x, plot_width, plot_height = 110, 930, 92
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}">',
           f'<rect width="{width}" height="{height}" fill="white"/>',
           '<style>text{font-family:Arial,sans-serif;fill:#1f2937}.axis{font-size:11px}'
           '.title{font-size:18px;font-weight:bold}.panel{font-size:13px;font-weight:bold}'
           '.legend{font-size:11px}</style>',
           f'<text class="title" x="24" y="29">{mode}: sustained MoQ/TCP coexistence</text>',
           '<text class="axis" x="24" y="47">60-second overlap; cwnd values are diagnostic byte-valued sender reports.</text>']
    panels = [
        ("Wire rate", (("foreground_wire_mbps", "Foreground MoQ", PLOT_COLORS["foreground"]),
                        ("tcp_wire_mbps", "Background TCP", PLOT_COLORS["tcp"])), "Mbit/s", False),
        ("Sender congestion window", (("foreground_cwnd_bytes", "Foreground MoQ", PLOT_COLORS["foreground"]),
                                       ("tcp_cwnd_bytes", "Background TCP", PLOT_COLORS["tcp"])), "bytes", False),
        ("Foreground RTT and Prague alpha", (("foreground_rtt_us", "RTT", PLOT_COLORS["tcp"]),
                                              ("prague_alpha", "Prague alpha", PLOT_COLORS["alpha"])), "separate scales", True),
        ("CE and ECN counters", (("foreground_ce_packets", "CE", PLOT_COLORS["alpha"]),
                                  ("foreground_ect0_packets", "ECT(0)", PLOT_COLORS["ect0"]),
                                  ("foreground_ect1_packets", "ECT(1)", PLOT_COLORS["ect1"])), "packets", False),
    ]
    for index, (title, series, units, separate_scales) in enumerate(panels):
        panel_y = header + index * panel_height
        graph_y = panel_y + 48
        svg.extend([
            f'<rect x="20" y="{panel_y + 4}" width="1140" height="{panel_height - 8}" '
            'fill="#ffffff" stroke="#cbd5e1"/>',
            f'<text class="panel" x="34" y="{panel_y + 24}">{title}</text>',
            f'<text class="axis" x="34" y="{panel_y + 40}">{units}</text>',
            f'<rect x="{plot_x}" y="{graph_y}" width="{plot_width}" height="{plot_height}" '
            'fill="#f8fafc" stroke="#94a3b8"/>',
            f'<text class="axis" x="{plot_x}" y="{graph_y + plot_height + 15}">0 s</text>',
            f'<text class="axis" x="{plot_x + plot_width - 30}" y="{graph_y + plot_height + 15}">59 s</text>',
        ])
        all_values = [[float(row[field]) for row in rows] for field, _, _ in series]
        shared_scale = max(max(values) for values in all_values) or 1.0
        legend_x = 360
        for series_index, ((field, label, color), values) in enumerate(zip(series, all_values)):
            scale = (max(values) or 1.0) if separate_scales else shared_scale
            svg.append(svg_polyline(values, plot_x, graph_y, plot_width, plot_height, scale, color))
            legend_y = panel_y + 23
            x = legend_x + series_index * 230
            max_label = f"max {max(values):.0f}" if separate_scales else f"max {shared_scale:.0f}"
            svg.extend([f'<line x1="{x}" y1="{legend_y - 4}" x2="{x + 16}" y2="{legend_y - 4}" '
                        f'stroke="{color}" stroke-width="2"/>',
                        f'<text class="legend" x="{x + 21}" y="{legend_y}">{label} ({max_label})</text>'])
        svg.append(f'<text class="axis" x="{plot_x - 76}" y="{graph_y + 11}">{shared_scale:.0f}</text>')
    svg.append("</svg>\n")
    (case / "timeline.svg").write_text("".join(svg), encoding="utf-8")


def render_aggregate_svg(root, summaries, aggregates):
    width, height = 1180, 630
    modes = list(MODES)
    labels = [MODES[mode] for mode in modes]
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}">',
           f'<rect width="{width}" height="{height}" fill="white"/>',
           '<style>text{font-family:Arial,sans-serif;fill:#1f2937}.axis{font-size:11px}'
           '.title{font-size:18px;font-weight:bold}.panel{font-size:13px;font-weight:bold}'
           '.value{font-size:10px}</style>',
           '<text class="title" x="24" y="29">Sustained MoQ/TCP coexistence: aggregate of three repetitions</text>',
           '<text class="axis" x="24" y="47">Bars are mode means; circles are individual repetitions.</text>']

    def panel(title, values_by_series, y, unit, maximum=None):
        chart_x, chart_y, chart_width, chart_height = 130, y + 47, 860, 108
        max_value = maximum or max(
            value for _, _, groups in values_by_series for group in groups for value in group
        ) or 1.0
        svg.extend([f'<rect x="20" y="{y}" width="1140" height="{chart_height + 69}" fill="#ffffff" stroke="#cbd5e1"/>',
                    f'<text class="panel" x="34" y="{y + 24}">{title}</text>',
                    f'<text class="axis" x="34" y="{y + 40}">{unit}; scale 0–{max_value:.1f}</text>',
                    f'<rect x="{chart_x}" y="{chart_y}" width="{chart_width}" height="{chart_height}" fill="#f8fafc" stroke="#94a3b8"/>'])
        group_width = chart_width / len(modes)
        series_width = min(36, group_width / (len(values_by_series) + 1))
        for mode_index, (mode, label) in enumerate(zip(modes, labels)):
            cx = chart_x + group_width * (mode_index + .5)
            svg.append(f'<text class="axis" text-anchor="middle" x="{cx:.1f}" y="{chart_y + chart_height + 17}">{label}</text>')
            for series_index, (name, color, values) in enumerate(values_by_series):
                mean = statistics.mean(values[mode_index])
                x = cx + (series_index - (len(values_by_series) - 1) / 2) * (series_width + 8) - series_width / 2
                bar_height = mean / max_value * chart_height
                svg.append(f'<rect x="{x:.1f}" y="{chart_y + chart_height - bar_height:.1f}" width="{series_width:.1f}" '
                           f'height="{bar_height:.1f}" fill="{color}" opacity=".78"/>')
                svg.append(f'<text class="value" text-anchor="middle" x="{x + series_width / 2:.1f}" '
                           f'y="{chart_y + chart_height - bar_height - 4:.1f}">{mean:.2f}</text>')
                for point_index, value in enumerate(values[mode_index]):
                    dot_x = x + series_width * (point_index + 1) / (len(values[mode_index]) + 1)
                    dot_y = chart_y + chart_height - value / max_value * chart_height
                    svg.append(f'<circle cx="{dot_x:.1f}" cy="{dot_y:.1f}" r="3" fill="white" stroke="{color}" stroke-width="1.5"/>')
        legend_x = 1015
        for index, (name, color, _) in enumerate(values_by_series):
            y_legend = chart_y + 18 + index * 20
            svg.extend([f'<rect x="{legend_x}" y="{y_legend - 9}" width="12" height="12" fill="{color}"/>',
                        f'<text class="axis" x="{legend_x + 18}" y="{y_legend}">{name}</text>'])

    def values(field):
        return [[row[field] for row in summaries if row["mode"] == mode] for mode in modes]

    panel("Mean forward wire rate", (("MoQ foreground", PLOT_COLORS["foreground"], values("foreground_wire_mbps_mean")),
                                      ("TCP background", PLOT_COLORS["tcp"], values("tcp_wire_mbps_mean"))), 65, "Mbit/s", 20.0)
    panel("Bottleneck use and foreground share", (("Bottleneck utilisation", PLOT_COLORS["utilisation"], values("bottleneck_utilization_percent_mean")),
                                                    ("Foreground share", PLOT_COLORS["foreground"], [[row["foreground_share_mean"] * 100 for row in summaries if row["mode"] == mode] for mode in modes])), 250, "percent", 100.0)
    panel("Maximum sender cwnd (diagnostic)", (("MoQ foreground", PLOT_COLORS["foreground"], values("foreground_cwnd_bytes_max")),
                                                 ("TCP background", PLOT_COLORS["tcp"], values("tcp_cwnd_bytes_max"))), 435, "bytes")
    svg.append("</svg>\n")
    (root / "aggregate.svg").write_text("".join(svg), encoding="utf-8")


def timeline_summary(case, metadata):
    with (case / "timeline.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    def mean(field):
        return statistics.mean(float(row[field]) for row in rows)
    return {
        "case": case.name,
        "mode": metadata["mode"],
        "repetition": metadata["repetition"],
        "bins": len(rows),
        "foreground_wire_mbps_mean": mean("foreground_wire_mbps"),
        "tcp_wire_mbps_mean": mean("tcp_wire_mbps"),
        "tcp_wire_active_bins": sum(float(row["tcp_wire_mbps"]) > 0 for row in rows),
        "combined_wire_mbps_mean": mean("combined_wire_mbps"),
        "bottleneck_utilization_percent_mean":
            mean("bottleneck_utilization_percent"),
        "foreground_share_mean": mean("foreground_share"),
        "foreground_cwnd_bytes_max":
            max(float(row["foreground_cwnd_bytes"]) for row in rows),
        "tcp_cwnd_bytes_max": max(float(row["tcp_cwnd_bytes"]) for row in rows),
        "ce_packets_final": int(rows[-1]["foreground_ce_packets"]),
        "ect0_packets_final": int(rows[-1]["foreground_ect0_packets"]),
        "ect1_packets_final": int(rows[-1]["foreground_ect1_packets"]),
        "subscriber_result": json.loads(
            (case / "subscriber-result.json").read_text()),
    }


def self_test():
    assert IP_ECN_CODES == {"ect0": 2, "ect1": 1, "ce": 3}
    assert [x["cwnd_bytes"] for x in resample(
        [{"time_us": 0, "cwnd_bytes": 1}, {"time_us": 1_500_000, "cwnd_bytes": 2}], 3)
        if x] == [1, 2]
    assert overlap_bins({"foreground_started_epoch": 5, "tcp_started_epoch": 0}, 2) == [5, 6]
    assert aligned_interval_index(100.0, 105.0, 6.2) == 1
    valid = [{"ect0_packets": 0, "ect1_packets": 0, "ce_packets": 0,
              "alpha_numerator": 0}]
    mode_checks("l4s-off", valid)
    try:
        mode_checks("l4s-ect0", valid)
    except ValueError:
        pass
    else:
        raise AssertionError("mode invariant failure was not detected")
    try:
        iperf_intervals(Path("/dev/null"), 0, 0, 1)
    except (ValueError, json.JSONDecodeError):
        pass
    else:
        raise AssertionError("invalid iperf input was not rejected")
    print("Sustained coexistence analyzer self-test: PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    root = Path(args.directory)
    summaries = []
    for case in sorted(path for path in root.iterdir() if path.is_dir()):
        metadata, foreground, tcp, foreground_wire, background_wire, issues = analyze_case(case)
        write_timeline(case, metadata, foreground, tcp, foreground_wire, background_wire)
        render_svg(case, MODES[metadata["mode"]])
        summary = timeline_summary(case, metadata)
        summary["acceptance_issues"] = issues
        summaries.append(summary)
    if len(summaries) != 9:
        raise SystemExit(f"expected nine completed cases, found {len(summaries)}")
    aggregates = {}
    for mode in MODES:
        mode_rows = [row for row in summaries if row["mode"] == mode]
        if len(mode_rows) != 3:
            raise SystemExit(f"{mode}: expected three repetitions")
        aggregates[mode] = {
            "repetitions": len(mode_rows),
            "tcp_wire_mbps_mean": statistics.mean(
                row["tcp_wire_mbps_mean"] for row in mode_rows),
            "foreground_wire_mbps_mean": statistics.mean(
                row["foreground_wire_mbps_mean"] for row in mode_rows),
            "combined_wire_mbps_mean": statistics.mean(
                row["combined_wire_mbps_mean"] for row in mode_rows),
            "foreground_share_mean": statistics.mean(
                row["foreground_share_mean"] for row in mode_rows),
        }
    failures = [f"{row['case']}: {issue}" for row in summaries
                for issue in row["acceptance_issues"]]
    render_aggregate_svg(root, summaries, aggregates)
    (root / "summary.json").write_text(
        json.dumps({"acceptance_passed": not failures, "acceptance_failures": failures,
                    "cases": summaries, "aggregates": aggregates},
                   indent=2) + "\n")
    if failures:
        raise SystemExit("sustained coexistence acceptance failed:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
