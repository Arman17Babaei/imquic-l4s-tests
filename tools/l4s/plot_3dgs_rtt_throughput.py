#!/usr/bin/env python3
"""Compare foreground payload goodput and smoothed RTT for two 3DGS runs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


STREAMS = (
    ("high-prague", "Prague stream (ECT(1), 25% assigned bytes)"),
    ("low-reno", "Reno stream (Not-ECT, 75% assigned bytes)"),
)


def payload_goodput(path: Path, *, bin_us: int = 1_000_000) -> list[float]:
    """Return received application payload goodput in fixed-width bins."""
    if bin_us <= 0:
        raise ValueError("bin_us must be positive")
    byte_bins: list[int] = []
    last_time = -1
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"arrival_time_us", "payload_bytes"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path}: arrival timeline lacks {sorted(required)}")
        for line_number, row in enumerate(reader, start=2):
            try:
                time_us = int(row["arrival_time_us"])
                payload_bytes = int(row["payload_bytes"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{path}:{line_number}: invalid arrival row") from error
            if time_us < last_time or payload_bytes <= 0:
                raise ValueError(f"{path}:{line_number}: invalid arrival ordering")
            last_time = time_us
            bin_index = time_us // bin_us
            while len(byte_bins) <= bin_index:
                byte_bins.append(0)
            byte_bins[bin_index] += payload_bytes
    return [payload_bytes * 8.0 / bin_us for payload_bytes in byte_bins]


def smoothed_rtt(path: Path) -> list[tuple[float, float]]:
    """Read transport-relative time and smoothed RTT in seconds and ms."""
    points: list[tuple[float, float]] = []
    last_time = -1
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"time_us", "rtt_us"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path}: transport metrics lack {sorted(required)}")
        for line_number, row in enumerate(reader, start=2):
            try:
                time_us = int(row["time_us"])
                rtt_us = int(row["rtt_us"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{path}:{line_number}: invalid RTT row") from error
            if time_us < last_time or rtt_us <= 0:
                raise ValueError(f"{path}:{line_number}: invalid RTT ordering")
            last_time = time_us
            points.append((time_us / 1_000_000.0, rtt_us / 1000.0))
    if not points:
        raise ValueError(f"{path}: transport metrics are empty")
    return points


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_configuration(root: Path) -> dict:
    return json.loads((root / "provenance.json").read_text(encoding="utf-8"))[
        "configuration"
    ]


def render_comparison(
    dualpi2_root: Path,
    classic_root: Path,
    output_dir: Path,
    *,
    case: str = "l4s-0p25-rep-01",
    bin_us: int = 1_000_000,
) -> dict:
    import matplotlib.pyplot as plt

    runs = (
        ("L4S→L4S (DualPI2 downstream)", dualpi2_root, "#1565c0"),
        ("L4S→Classic (FIFO downstream)", classic_root, "#d84315"),
    )
    configurations = [run_configuration(root) for _, root, _ in runs]
    differing = {
        key: [configuration.get(key) for configuration in configurations]
        for key in sorted(set(configurations[0]) | set(configurations[1]))
        if configurations[0].get(key) != configurations[1].get(key)
    }
    if differing != {"downstream_mode": ["dualpi2", "classic"]}:
        raise ValueError(f"runs differ beyond downstream mode: {differing}")

    figure, axes = plt.subplots(2, 2, figsize=(13.2, 8.0), sharex=True)
    source_records = []
    maximum_time = 0.0
    for row_index, (stream_name, _) in enumerate(STREAMS):
        rate_axis, rtt_axis = axes[row_index]
        for label, root, color in runs:
            stream_root = root / case / stream_name
            arrival_path = stream_root / "arrival-timeline.csv"
            metrics_path = stream_root / "transport-metrics.csv"
            rates = payload_goodput(arrival_path, bin_us=bin_us)
            edges = [index * bin_us / 1_000_000.0 for index in range(len(rates) + 1)]
            rate_axis.stairs(rates, edges, label=label, color=color, linewidth=1.8)
            rtt = smoothed_rtt(metrics_path)
            rtt_axis.plot(
                [point[0] for point in rtt],
                [point[1] for point in rtt],
                label=label,
                color=color,
                linewidth=1.35,
            )
            maximum_time = max(maximum_time, edges[-1], rtt[-1][0])
            source_records.append(
                {
                    "run": root.name,
                    "downstream_mode": run_configuration(root)["downstream_mode"],
                    "stream": stream_name,
                    "arrival_timeline": str(arrival_path),
                    "arrival_timeline_sha256": sha256(arrival_path),
                    "transport_metrics": str(metrics_path),
                    "transport_metrics_sha256": sha256(metrics_path),
                }
            )

        short_name = (
            "Prague (ECT(1), 25%)"
            if stream_name == "high-prague"
            else "Reno (Not-ECT, 75%)"
        )
        rate_axis.set_title(f"{short_name}: received payload goodput")
        rate_axis.set_ylabel("Payload goodput (Mbit/s)")
        rtt_axis.set_title(f"{short_name}: transport smoothed RTT")
        rtt_axis.set_ylabel("Smoothed RTT (ms)")
        for axis in (rate_axis, rtt_axis):
            axis.grid(True, alpha=0.25)
            axis.set_ylim(bottom=0)

    axes[1, 0].set_xlabel("Time since workload start (s)")
    axes[1, 1].set_xlabel("Time since workload start (s)")
    for axis in axes.flat:
        axis.set_xlim(0, maximum_time)

    handles, labels = axes[0, 0].get_legend_handles_labels()
    figure.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.94),
        ncol=2,
        frameon=False,
    )
    figure.suptitle(
        "3DGS foreground transport: DualPI2 vs Classic downstream",
        fontsize=15,
        y=0.99,
    )
    figure.text(
        0.5,
        0.012,
        "One repetition; 20 ms configured base RTT; 50 Mbit/s downstream; "
        "280 Mbit/s Reno background. Goodput excludes protocol overhead.",
        ha="center",
        fontsize=9,
    )
    figure.tight_layout(rect=(0.02, 0.045, 0.99, 0.87))

    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / "rtt-throughput-comparison.svg"
    png_path = output_dir / "rtt-throughput-comparison.png"
    figure.savefig(svg_path)
    figure.savefig(png_path, dpi=180)
    plt.close(figure)

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "case": case,
        "comparison": "dualpi2 downstream versus classic FIFO downstream",
        "controlled_configuration_difference": differing,
        "definitions": {
            "throughput": (
                "received application payload bytes in fixed one-second bins; "
                "Mbit/s; excludes QUIC/IP/link overhead"
            ),
            "rtt": "transport-reported smoothed RTT; not direct queue delay",
            "time_origin": "per-run workload/transport-relative origin",
            "repetitions": 1,
        },
        "outputs": {"svg": str(svg_path), "png": str(png_path)},
        "sources": source_records,
    }
    manifest_path = output_dir / "rtt-throughput-comparison.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dualpi2_root", type=Path)
    parser.add_argument("classic_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", default="l4s-0p25-rep-01")
    args = parser.parse_args()
    render_comparison(
        args.dualpi2_root, args.classic_root, args.output, case=args.case
    )


if __name__ == "__main__":
    main()
