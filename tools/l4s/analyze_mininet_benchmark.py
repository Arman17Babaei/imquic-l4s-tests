#!/usr/bin/env python3
"""Analyze and plot repeated Mininet ECN benchmark runs."""

import argparse
import csv
import html
import json
import re
import statistics
import subprocess
import tempfile
from pathlib import Path

from analyze_timeseries import AnalysisError, load_samples


MODE_ORDER = ("l4s-off", "l4s-ect0", "l4s-on")
MODE_LABELS = {
    "l4s-off": "Reno / Not-ECT",
    "l4s-ect0": "Reno / ECT(0)",
    "l4s-on": "Prague / ECT(1)",
}
MODE_COLORS = {
    "l4s-off": "#c44e52",
    "l4s-ect0": "#e69f00",
    "l4s-on": "#0072b2",
}
ROW_FIELDS = (
    "mode",
    "repetition",
    "background_target_mbps",
    "background_actual_mbps",
    "quic_goodput_mbps",
    "quic_wire_mbps",
    "background_wire_mbps",
    "combined_wire_mbps",
    "bottleneck_utilization_percent",
    "duration_ms",
    "final_rtt_us",
    "final_cwnd_bytes",
    "final_ect1_packets",
    "final_ce_packets",
    "captured_ect0_packets",
    "captured_ect1_packets",
    "captured_ce_packets",
    "background_tcp_ecn_packets",
    "quic_forward_drops",
    "background_forward_drops",
    "dualpi2_forward_drops",
    "dualpi2_l4s_packets",
    "dualpi2_ecn_marks",
)
FLOAT_FIELDS = {
    "background_actual_mbps",
    "quic_goodput_mbps",
    "quic_wire_mbps",
    "background_wire_mbps",
    "combined_wire_mbps",
    "bottleneck_utilization_percent",
    "duration_ms",
}


def packet_count(path, display_filter):
    result = subprocess.run(
        [
            "tshark", "-r", str(path), "-Y", display_filter,
            "-T", "fields", "-e", "frame.number",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return len(result.stdout.splitlines())


def packet_bytes(path, display_filter):
    result = subprocess.run(
        [
            "tshark", "-r", str(path), "-Y", display_filter,
            "-T", "fields", "-e", "ip.len",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return sum(
        int(value.split(",", 1)[0])
        for value in result.stdout.splitlines()
        if value
    )


def packet_deficit(ingress_packets, egress_packets):
    """Return packets seen before, but not after, the forward bottleneck."""
    return max(0, ingress_packets - egress_packets)


def inferred_forward_drops(ingress_capture, egress_capture, display_filter):
    return packet_deficit(
        packet_count(ingress_capture, display_filter),
        packet_count(egress_capture, display_filter),
    )


def tc_totals(path):
    text = Path(path).read_text(encoding="utf-8")
    l4s = sum(int(value) for value in re.findall(r"pkts_in_l\s+(\d+)", text))
    marked = sum(int(value) for value in re.findall(r"ecn_mark\s+(\d+)", text))
    return l4s, marked


def tc_interface_drops(path, interface):
    text = Path(path).read_text(encoding="utf-8")
    device = re.search(
        rf"^device={re.escape(interface)}\n(.*?)(?=^device=|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if device is None:
        raise AnalysisError(f"{path}: missing tc statistics for {interface}")
    dropped = re.search(
        r"^qdisc dualpi2\b.*?\n"
        r"\s*Sent\b.*?\(dropped\s+(\d+),",
        device.group(1),
        re.MULTILINE | re.DOTALL,
    )
    if dropped is None:
        raise AnalysisError(f"{path}: missing DualPI2 drops for {interface}")
    return int(dropped.group(1))


def iperf_rate(path):
    if not Path(path).exists():
        return 0
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return int(data["end"]["sum_received"]["bits_per_second"])


def analyze_case(directory):
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    samples = load_samples(directory / "metrics.csv")
    if len(samples) < 2:
        raise AnalysisError(f"{directory.name}: insufficient metrics samples")
    duration_us = samples[-1]["time_us"] - samples[0]["time_us"]
    if duration_us <= 0:
        raise AnalysisError(f"{directory.name}: invalid duration")
    ect1_capture = packet_count(
        directory / "switch-client.pcap", "udp.port == 4443 && ip.dsfield.ecn == 1"
    )
    ect0_capture = packet_count(
        directory / "switch-client.pcap", "udp.port == 4443 && ip.dsfield.ecn == 2"
    )
    ce_capture = packet_count(
        directory / "switch-server.pcap", "udp.port == 4443 && ip.dsfield.ecn == 3"
    )
    tcp_ecn_capture = packet_count(
        directory / "switch-client.pcap", "tcp.port == 5201 && ip.dsfield.ecn != 0"
    )
    forward_quic_filter = (
        f"udp.dstport == 4443 && ip.dst == {metadata['server_ip']}"
    )
    quic_forward_drops = inferred_forward_drops(
        directory / "switch-client.pcap",
        directory / "switch-server.pcap",
        forward_quic_filter,
    )
    background_forward_drops = packet_count(
        directory / "switch-client.pcap",
        f"tcp.dstport == 5201 && ip.dst == {metadata['server_ip']} && "
        "(tcp.analysis.retransmission || tcp.analysis.fast_retransmission)",
    )
    dualpi2_forward_drops = tc_interface_drops(
        directory / "dualpi2-stats.txt", "s1-eth2"
    )
    wall_duration = (
        metadata["quic_finished_epoch"] - metadata["quic_started_epoch"]
    )
    if wall_duration <= 0:
        raise AnalysisError(f"{directory.name}: invalid wall-clock duration")
    interval = (
        f"frame.time_epoch >= {metadata['quic_started_epoch']:.6f} && "
        f"frame.time_epoch <= {metadata['quic_finished_epoch']:.6f} && "
        f"ip.dst == {metadata['server_ip']}"
    )
    server_capture = directory / "switch-server.pcap"
    quic_wire_bytes = packet_bytes(
        server_capture, f"{interval} && udp.dstport == 4443"
    )
    background_wire_bytes = packet_bytes(
        server_capture, f"{interval} && tcp.dstport == 5201"
    )
    quic_wire_mbps = quic_wire_bytes * 8 / wall_duration / 1e6
    background_wire_mbps = background_wire_bytes * 8 / wall_duration / 1e6
    combined_wire_mbps = quic_wire_mbps + background_wire_mbps
    l4s_packets, ecn_marks = tc_totals(directory / "dualpi2-stats.txt")
    final = samples[-1]
    row = {
        "mode": metadata["mode"],
        "repetition": metadata["repetition"],
        "background_target_mbps": metadata["background_mbps"],
        "background_actual_mbps": iperf_rate(directory / "iperf-client.json") / 1e6,
        "quic_goodput_mbps": metadata["transfer_bytes"] * 8 / duration_us,
        "quic_wire_mbps": quic_wire_mbps,
        "background_wire_mbps": background_wire_mbps,
        "combined_wire_mbps": combined_wire_mbps,
        "bottleneck_utilization_percent": (
            combined_wire_mbps / metadata["bottleneck_mbps"] * 100
        ),
        "duration_ms": duration_us / 1000,
        "final_rtt_us": final["rtt_us"],
        "final_cwnd_bytes": final["cwnd_bytes"],
        "final_ect1_packets": final["ect1_packets"],
        "final_ce_packets": final["ce_packets"],
        "captured_ect0_packets": ect0_capture,
        "captured_ect1_packets": ect1_capture,
        "captured_ce_packets": ce_capture,
        "background_tcp_ecn_packets": tcp_ecn_capture,
        "quic_forward_drops": quic_forward_drops,
        "background_forward_drops": background_forward_drops,
        "dualpi2_forward_drops": dualpi2_forward_drops,
        "dualpi2_l4s_packets": l4s_packets,
        "dualpi2_ecn_marks": ecn_marks,
    }
    if metadata["mode"] == "l4s-on":
        if (final["ect1_packets"] == 0 or ect1_capture == 0 or
                ect0_capture != 0 or l4s_packets == 0):
            raise AnalysisError(f"{directory.name}: L4S-on run has no ECT(1) evidence")
        if final["ce_packets"] == 0 or ce_capture == 0 or ecn_marks == 0:
            raise AnalysisError(f"{directory.name}: L4S-on run has no CE evidence")
    elif metadata["mode"] == "l4s-ect0":
        if final["ect1_packets"] != 0 or ect1_capture != 0 or ect0_capture == 0:
            raise AnalysisError(f"{directory.name}: ECT(0) run has invalid ECN marking")
        if final["ce_packets"] == 0 or ce_capture == 0 or ecn_marks == 0:
            raise AnalysisError(f"{directory.name}: ECT(0) run has no CE feedback")
    elif metadata["mode"] == "l4s-off":
        if (final["ect1_packets"] != 0 or final["ce_packets"] != 0 or
                ect0_capture != 0 or ect1_capture != 0):
            raise AnalysisError(f"{directory.name}: L4S-off run unexpectedly used ECN")
    else:
        raise AnalysisError(f"{directory.name}: unknown mode")
    target = metadata["background_mbps"]
    if tcp_ecn_capture != 0:
        raise AnalysisError(f"{directory.name}: background TCP was ECN-capable")
    if target > 0 and row["background_actual_mbps"] < target * 0.5:
        raise AnalysisError(f"{directory.name}: background TCP missed requested rate")
    return row


def validate_matrix(rows, expected_repetitions, expected_modes):
    cases = {}
    for row in rows:
        key = (
            row["background_target_mbps"], row["mode"], row["repetition"]
        )
        if key in cases:
            raise AnalysisError(f"duplicate benchmark case: {key}")
        cases[key] = row
    rates = sorted({row["background_target_mbps"] for row in rows})
    expected_repeats = set(range(1, expected_repetitions + 1))
    for rate in rates:
        for mode in expected_modes:
            actual = {
                row["repetition"] for row in rows
                if row["background_target_mbps"] == rate and row["mode"] == mode
            }
            if actual != expected_repeats:
                raise AnalysisError(
                    f"background {rate} Mbps {mode} repetitions are "
                    f"{sorted(actual)}, expected {sorted(expected_repeats)}"
                )


def load_summary(path):
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as stream:
        for record in csv.DictReader(stream):
            row = {}
            for field in ROW_FIELDS:
                value = record.get(field, "0")
                if field == "mode":
                    row[field] = value
                elif field in FLOAT_FIELDS:
                    row[field] = float(value)
                else:
                    row[field] = int(value)
            rows.append(row)
    if not rows:
        raise AnalysisError(f"reference summary is empty: {path}")
    return rows


def analyze(root):
    benchmark = json.loads(
        (Path(root) / "benchmark.json").read_text(encoding="utf-8")
    )
    rows = [analyze_case(path) for path in sorted(Path(root).glob("l4s-*-bg-*"))]
    if not rows:
        raise AnalysisError("no benchmark cases found")
    modes = tuple(benchmark["modes"])
    validate_matrix(rows, benchmark["repetitions"], modes)
    for mode in modes:
        peak = max(
            row["bottleneck_utilization_percent"]
            for row in rows if row["mode"] == mode
        )
        if peak < 70:
            raise AnalysisError(
                f"{mode} never visibly exercised the bottleneck ({peak:.1f}% peak)"
            )
    return rows, benchmark


AGGREGATE_METRICS = (
    "background_actual_mbps",
    "quic_goodput_mbps",
    "quic_wire_mbps",
    "background_wire_mbps",
    "combined_wire_mbps",
    "bottleneck_utilization_percent",
    "final_rtt_us",
    "final_cwnd_bytes",
    "final_ect1_packets",
    "final_ce_packets",
    "captured_ect0_packets",
    "captured_ect1_packets",
    "captured_ce_packets",
    "quic_forward_drops",
    "background_forward_drops",
    "dualpi2_forward_drops",
    "dualpi2_ecn_marks",
)


def aggregate_rows(rows):
    aggregates = []
    keys = sorted({
        (row["background_target_mbps"], row["mode"]) for row in rows
    })
    for rate, mode in keys:
        group = [
            row for row in rows
            if row["background_target_mbps"] == rate and row["mode"] == mode
        ]
        aggregate = {
            "mode": mode,
            "background_target_mbps": rate,
            "repetitions": len(group),
        }
        for metric in AGGREGATE_METRICS:
            values = [row[metric] for row in group]
            aggregate[f"{metric}_mean"] = statistics.mean(values)
            aggregate[f"{metric}_stdev"] = (
                statistics.stdev(values) if len(values) > 1 else 0.0
            )
        aggregates.append(aggregate)
    return aggregates


def render_plot(path, aggregates, benchmark):
    panels = (
        ("combined_wire_mbps", "Concurrent bottleneck load", "Mbit/s", True),
        ("quic_goodput_mbps", "QUIC application goodput", "Mbit/s", False),
        ("final_rtt_us", "Final smoothed RTT", "microseconds", False),
        ("final_ce_packets", "QUIC CE feedback", "packets", False),
        ("quic_forward_drops", "Inferred forward QUIC drops", "packets", False),
        (
            "background_forward_drops",
            "Inferred forward background TCP drops",
            "packets",
            False,
        ),
    )
    panel_rows = (len(panels) + 1) // 2
    width, height = 1200, 140 + panel_rows * 350
    rates = sorted({row["background_target_mbps"] for row in aggregates})
    modes = [mode for mode in MODE_ORDER if any(
        row["mode"] == mode for row in aggregates
    )]
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:DejaVu Sans,Arial,sans-serif;fill:#222}'
        '.grid{stroke:#ddd;stroke-width:1}.axis{stroke:#333;stroke-width:1.5}'
        '.series{fill:none;stroke-width:3}.error{stroke-width:1.5}'
        '.point{stroke:white;stroke-width:1.5}</style>',
        '<text x="600" y="34" text-anchor="middle" font-size="22" '
        'font-weight="bold">Mininet L4S coexistence benchmark</text>',
        f'<text x="600" y="58" text-anchor="middle" font-size="14">'
        f'{benchmark["repetitions"]} repetitions; '
        f'{html.escape(benchmark["bottleneck"])} DualPI2 bottleneck; '
        'error bars show ±1 standard deviation</text>',
        '<text x="600" y="78" text-anchor="middle" font-size="12">'
        'QUIC drops are the capture deficit across the forward bottleneck; '
        'TCP drops are inferred from retransmissions</text>',
    ]
    for index, (metric, title, unit, show_bottleneck) in enumerate(panels):
        panel_x = 70 + (index % 2) * 575
        panel_y = 120 + (index // 2) * 350
        plot_x, plot_y = panel_x + 65, panel_y + 35
        plot_w, plot_h = 455, 245
        values = [
            row[f"{metric}_mean"] + row[f"{metric}_stdev"]
            for row in aggregates
        ]
        if show_bottleneck:
            values.append(benchmark["bottleneck_mbps"])
        y_max = max(values) * 1.15 if max(values) > 0 else 1.0
        x_min, x_max = min(rates), max(rates)

        def x_coord(rate):
            if x_max == x_min:
                return plot_x + plot_w / 2
            return plot_x + (rate - x_min) / (x_max - x_min) * plot_w

        def y_coord(value):
            return plot_y + plot_h - value / y_max * plot_h

        svg.append(
            f'<text x="{panel_x + 292}" y="{panel_y + 16}" '
            f'text-anchor="middle" font-size="16" font-weight="bold">'
            f'{html.escape(title)}</text>'
        )
        for tick in range(5):
            value = y_max * tick / 4
            y = y_coord(value)
            svg.extend((
                f'<line class="grid" x1="{plot_x}" y1="{y:.1f}" '
                f'x2="{plot_x + plot_w}" y2="{y:.1f}"/>',
                f'<text x="{plot_x - 9}" y="{y + 5:.1f}" text-anchor="end" '
                f'font-size="11">{value:.1f}</text>',
            ))
        svg.extend((
            f'<line class="axis" x1="{plot_x}" y1="{plot_y}" '
            f'x2="{plot_x}" y2="{plot_y + plot_h}"/>',
            f'<line class="axis" x1="{plot_x}" y1="{plot_y + plot_h}" '
            f'x2="{plot_x + plot_w}" y2="{plot_y + plot_h}"/>',
            f'<text x="{panel_x + 12}" y="{plot_y + plot_h / 2}" '
            f'transform="rotate(-90 {panel_x + 12} {plot_y + plot_h / 2})" '
            f'text-anchor="middle" font-size="12">{html.escape(unit)}</text>',
            f'<text x="{plot_x + plot_w / 2}" y="{plot_y + plot_h + 42}" '
            f'text-anchor="middle" font-size="12">Classic TCP target (Mbit/s)</text>',
        ))
        for rate in rates:
            x = x_coord(rate)
            svg.append(
                f'<text x="{x:.1f}" y="{plot_y + plot_h + 20}" '
                f'text-anchor="middle" font-size="11">{rate}</text>'
            )
        if show_bottleneck:
            y = y_coord(benchmark["bottleneck_mbps"])
            svg.extend((
                f'<line x1="{plot_x}" y1="{y:.1f}" x2="{plot_x + plot_w}" '
                f'y2="{y:.1f}" stroke="#222" stroke-width="2" '
                'stroke-dasharray="8 5"/>',
                f'<text x="{plot_x + plot_w - 4}" y="{y - 7:.1f}" '
                f'text-anchor="end" font-size="11">configured bottleneck '
                f'{benchmark["bottleneck_mbps"]:g} Mbit/s</text>',
            ))
        for mode in modes:
            series = sorted(
                (row for row in aggregates if row["mode"] == mode),
                key=lambda row: row["background_target_mbps"],
            )
            points = " ".join(
                f'{x_coord(row["background_target_mbps"]):.1f},'
                f'{y_coord(row[f"{metric}_mean"]):.1f}'
                for row in series
            )
            svg.append(
                f'<polyline class="series" stroke="{MODE_COLORS[mode]}" '
                f'points="{points}"/>'
            )
            for row in series:
                x = x_coord(row["background_target_mbps"])
                mean = row[f"{metric}_mean"]
                deviation = row[f"{metric}_stdev"]
                y_low = y_coord(max(0, mean - deviation))
                y_high = y_coord(mean + deviation)
                y = y_coord(mean)
                svg.extend((
                    f'<line class="error" stroke="{MODE_COLORS[mode]}" x1="{x:.1f}" '
                    f'y1="{y_low:.1f}" x2="{x:.1f}" y2="{y_high:.1f}"/>',
                    f'<line class="error" stroke="{MODE_COLORS[mode]}" '
                    f'x1="{x - 5:.1f}" y1="{y_low:.1f}" x2="{x + 5:.1f}" '
                    f'y2="{y_low:.1f}"/>',
                    f'<line class="error" stroke="{MODE_COLORS[mode]}" '
                    f'x1="{x - 5:.1f}" y1="{y_high:.1f}" x2="{x + 5:.1f}" '
                    f'y2="{y_high:.1f}"/>',
                    f'<circle class="point" cx="{x:.1f}" cy="{y:.1f}" r="5" '
                    f'fill="{MODE_COLORS[mode]}"/>',
                ))
    legend_width = 230
    legend_start = (width - legend_width * len(modes)) / 2
    for index, mode in enumerate(modes):
        x = legend_start + index * legend_width
        svg.extend((
            f'<line x1="{x}" y1="{height - 25}" x2="{x + 32}" '
            f'y2="{height - 25}" '
            f'stroke="{MODE_COLORS[mode]}" stroke-width="4"/>',
            f'<text x="{x + 40}" y="{height - 20}" font-size="13">'
            f'{MODE_LABELS[mode]}</text>',
        ))
    svg.append("</svg>")
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def write_results(root, rows, benchmark):
    with (root / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=ROW_FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    aggregates = aggregate_rows(rows)
    aggregate_fields = list(aggregates[0])
    with (root / "aggregate.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=aggregate_fields, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(aggregates)
    aggregate_index = {
        (row["background_target_mbps"], row["mode"]): row
        for row in aggregates
    }
    comparisons = []
    for rate in sorted({row["background_target_mbps"] for row in rows}):
        modes = {}
        for mode in MODE_ORDER:
            aggregate = aggregate_index.get((rate, mode))
            if aggregate is None:
                continue
            modes[mode] = {
                "goodput_mbps_mean": aggregate["quic_goodput_mbps_mean"],
                "goodput_mbps_stdev": aggregate["quic_goodput_mbps_stdev"],
                "final_rtt_us_mean": aggregate["final_rtt_us_mean"],
                "final_rtt_us_stdev": aggregate["final_rtt_us_stdev"],
                "ce_feedback_mean": aggregate["final_ce_packets_mean"],
                "quic_forward_drops_mean": (
                    aggregate["quic_forward_drops_mean"]
                ),
                "background_forward_drops_mean": (
                    aggregate["background_forward_drops_mean"]
                ),
                "bottleneck_utilization_percent_mean": (
                    aggregate["bottleneck_utilization_percent_mean"]
                ),
            }
        comparisons.append({
            "background_target_mbps": rate,
            "modes": modes,
        })
    result = {
        "status": "pass",
        "cases": len(rows),
        "repetitions": benchmark["repetitions"],
        "bottleneck_mbps": benchmark["bottleneck_mbps"],
        "background_rates_mbps": sorted({row["background_target_mbps"] for row in rows}),
        "aggregates": aggregates,
        "comparisons": comparisons,
        "rows": rows,
    }
    (root / "analysis.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    render_plot(root / "comparison.svg", aggregates, benchmark)
    print(json.dumps({
        "status": result["status"],
        "cases": result["cases"],
        "repetitions": result["repetitions"],
        "bottleneck_mbps": result["bottleneck_mbps"],
        "background_rates_mbps": result["background_rates_mbps"],
        "plot": str(root / "comparison.svg"),
    }, indent=2, sort_keys=True))


def self_test():
    if packet_deficit(120, 97) != 23 or packet_deficit(97, 120) != 0:
        raise AnalysisError("packet-drop inference self-test failed")
    with tempfile.TemporaryDirectory() as directory:
        tc_stats = Path(directory) / "dualpi2-stats.txt"
        tc_stats.write_text(
            "device=s1-eth1\n"
            "qdisc dualpi2 10: parent 1:1\n"
            " Sent 100 bytes 10 pkt (dropped 3, overlimits 0 requeues 0)\n"
            "device=s1-eth2\n"
            "qdisc dualpi2 10: parent 1:1\n"
            " Sent 200 bytes 20 pkt (dropped 7, overlimits 0 requeues 0)\n",
            encoding="utf-8",
        )
        if tc_interface_drops(tc_stats, "s1-eth2") != 7:
            raise AnalysisError("DualPI2 directional-drop self-test failed")
    rows = []
    for rate in (0, 10):
        for mode in MODE_ORDER:
            for repetition in range(1, 6):
                row = {
                    "mode": mode,
                    "repetition": repetition,
                    "background_target_mbps": rate,
                }
                for metric in AGGREGATE_METRICS:
                    row[metric] = rate + repetition + MODE_ORDER.index(mode)
                rows.append(row)
    validate_matrix(rows, 5, MODE_ORDER)
    aggregates = aggregate_rows(rows)
    if len(aggregates) != 6 or aggregates[0]["repetitions"] != 5:
        raise AnalysisError("benchmark aggregation self-test failed")
    benchmark = {
        "repetitions": 5,
        "bottleneck": "20mbit",
        "bottleneck_mbps": 20.0,
        "modes": {mode: MODE_LABELS[mode] for mode in MODE_ORDER},
    }
    with tempfile.TemporaryDirectory() as directory:
        plot = Path(directory) / "comparison.svg"
        render_plot(plot, aggregates, benchmark)
        if not plot.read_text(encoding="utf-8").startswith("<svg"):
            raise AnalysisError("benchmark plot self-test failed")
    print("Mininet benchmark analyzer self-test: PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="?", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--reference-summary", type=Path)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.results is None:
        parser.error("results directory is required")
    try:
        rows, benchmark = analyze(args.results)
        if args.reference_summary is not None:
            rows = load_summary(args.reference_summary) + rows
            modes = tuple(
                mode for mode in MODE_ORDER
                if any(row["mode"] == mode for row in rows)
            )
            benchmark["modes"] = {
                mode: MODE_LABELS[mode] for mode in modes
            }
            validate_matrix(rows, benchmark["repetitions"], modes)
        write_results(args.results, rows, benchmark)
    except (AnalysisError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Mininet L4S benchmark: FAIL: {exc}") from exc


if __name__ == "__main__":
    main()
