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


def render_svg(case, mode):
    with (case / "timeline.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    width, panel = 1000, 180
    def polyline(field, x0, y0, height, color):
        values = [float(row[field]) for row in rows]
        scale = max(max(values), 1.0)
        points = " ".join(f"{x0 + i * 900 / max(len(values) - 1, 1):.1f},"
                          f"{y0 + height - value / scale * height:.1f}"
                          for i, value in enumerate(values))
        return f'<polyline fill="none" stroke="{color}" points="{points}"/>'
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{panel * 4}">',
           f'<text x="20" y="24" font-size="18">{mode}: sustained MoQ/TCP coexistence</text>']
    panels = [
        ("wire rate (foreground and TCP)", (("foreground_wire_mbps", "#c44e52"),
                                             ("tcp_wire_mbps", "#0072b2"))),
        ("cwnd bytes (diagnostic; controllers are not semantically identical)",
         (("foreground_cwnd_bytes", "#c44e52"), ("tcp_cwnd_bytes", "#0072b2"))),
        ("foreground RTT (us) and Prague alpha",
         (("foreground_rtt_us", "#0072b2"), ("prague_alpha", "#e69f00"))),
        ("CE and ECN counters",
         (("foreground_ce_packets", "#e69f00"),
          ("foreground_ect0_packets", "#9467bd"),
          ("foreground_ect1_packets", "#2ca02c"))),
    ]
    for index, (label, series) in enumerate(panels):
        y = index * panel + 40
        svg.append(f'<text x="20" y="{y + 18}">{label}</text>')
        svg.append(f'<line x1="50" y1="{y + 30}" x2="950" y2="{y + 30}" stroke="#888"/>')
        for field, color in series:
            svg.append(polyline(field, 50, y + 35, 120, color))
    svg.append("</svg>\n")
    (case / "timeline.svg").write_text("".join(svg), encoding="utf-8")


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
    (root / "summary.json").write_text(
        json.dumps({"acceptance_passed": not failures, "acceptance_failures": failures,
                    "cases": summaries, "aggregates": aggregates},
                   indent=2) + "\n")
    if failures:
        raise SystemExit("sustained coexistence acceptance failed:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
