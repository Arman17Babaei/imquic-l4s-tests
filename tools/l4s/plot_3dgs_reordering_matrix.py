#!/usr/bin/env python3
"""Plot a matched DualPI2/Classic 3DGS Prague-share matrix."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from plot_3dgs_rtt_throughput import payload_goodput, run_configuration, sha256, smoothed_rtt


RUNS = (
    ("dualpi2", "L4S→L4S", "#1565c0"),
    ("classic", "L4S→Classic", "#d84315"),
)
STREAMS = (
    ("high-prague", "Prague", "#1565c0"),
    ("low-reno", "Reno", "#ef6c00"),
)


def _tag(fraction: float) -> str:
    return f"{fraction:.4f}".rstrip("0").rstrip(".").replace(".", "p")


def _analysis_cases(root: Path) -> dict[float, dict]:
    data = json.loads((root / "reordering-analysis.json").read_text(encoding="utf-8"))
    return {
        float(case["acceptance"]["requested_l4s_byte_fraction"]): case
        for case in data["cases"]
    }


def _cell_root(root: Path, fraction: float) -> Path:
    isolated = root / f"fraction-{_tag(fraction)}"
    return isolated if isolated.is_dir() else root


def _matrix_configuration(root: Path, fractions: list[float]) -> dict:
    return run_configuration(_cell_root(root, fractions[0]))


def overtaking_metrics(case: dict) -> tuple[float, float]:
    """Return all-packet mean and total overtaken Classic byte-pairs."""
    counts = case["capture_counts"]
    reorder = case["provider_reordering"]
    total_prague = int(counts["prague_provider_matched_packets"])
    overtaking_prague = int(reorder["prague_packets_with_overtake"])
    conditional_mean = reorder["overtaken_payload_bytes_per_prague_packet"].get("mean")
    total = 0.0 if conditional_mean is None else float(conditional_mean) * overtaking_prague
    mean = total / total_prague if total_prague else 0.0
    return mean, total


def _mean_rtt_ms(path: Path) -> float | None:
    points = smoothed_rtt(path)
    return statistics.fmean(value for _, value in points) if points else None


def _experiment_goodput(path: Path, stream: str, *, bin_us: int) -> list[float]:
    bins: list[int] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["path"] != stream:
                continue
            time_us = int(row["experiment_time_us"])
            if time_us < 0:
                continue
            index = time_us // bin_us
            while len(bins) <= index:
                bins.append(0)
            bins[index] += int(row["payload_bytes"])
    return [value * 8.0 / bin_us for value in bins]


def _measured_rtt(
    metrics_path: Path,
    publisher_result_path: Path,
    workload_start_epoch_us: int,
    deadline_s: float,
) -> list[tuple[float, float]]:
    publisher = json.loads(publisher_result_path.read_text(encoding="utf-8"))
    offset_s = (
        int(publisher["publisher_started_epoch_us"]) - workload_start_epoch_us
    ) / 1_000_000.0
    return [
        (time_s + offset_s, value)
        for time_s, value in smoothed_rtt(metrics_path)
        if 0.0 <= time_s + offset_s <= deadline_s
    ]


def _write_summary(rows: list[dict], destination: Path) -> None:
    fields = tuple(rows[0])
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _render_summary(rows: list[dict], output_dir: Path) -> list[Path]:
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 2, figsize=(13.5, 8.5), sharex=True)
    for mode, label, color in RUNS:
        selected = sorted(
            (row for row in rows if row["downstream_mode"] == mode),
            key=lambda row: row["requested_prague_share"],
        )
        shares = [100.0 * row["requested_prague_share"] for row in selected]
        axes[0, 0].plot(
            shares,
            [row["mean_overtaken_bytes_per_l4s_packet"] for row in selected],
            marker="o", label=label, color=color,
        )
        axes[0, 1].plot(
            shares,
            [row["total_overtaken_byte_pairs"] / 1_000_000_000.0 for row in selected],
            marker="o", label=label, color=color,
        )
        for stream, stream_label, stream_color in STREAMS:
            linestyle = "-" if mode == "dualpi2" else "--"
            axes[1, 0].plot(
                shares,
                [row[f"{stream}_mean_goodput_mbps"] for row in selected],
                marker="o", color=stream_color, linestyle=linestyle,
                label=f"{label} · {stream_label}",
            )
            axes[1, 1].plot(
                shares,
                [row[f"{stream}_mean_rtt_ms"] for row in selected],
                marker="o", color=stream_color, linestyle=linestyle,
                label=f"{label} · {stream_label}",
            )

    axes[0, 0].set_title("Mean Classic bytes overtaken per L4S packet")
    axes[0, 0].set_ylabel("Bytes / matched L4S packet")
    axes[0, 1].set_title("Total overtaken Classic byte-pairs")
    axes[0, 1].set_ylabel("GB of byte-pairs")
    axes[1, 0].set_title("Mean received payload goodput over measured workload")
    axes[1, 0].set_ylabel("Mbit/s")
    axes[1, 1].set_title("Mean transport-reported smoothed RTT")
    axes[1, 1].set_ylabel("ms")
    for axis in axes.flat:
        axis.set_xlabel("Assigned Prague payload share (%)")
        axis.set_xticks([0, 25, 50, 75, 100])
        axis.set_ylim(bottom=0)
        axis.grid(True, alpha=0.25)
    axes[0, 0].legend(frameon=False)
    axes[0, 1].legend(frameon=False)
    axes[1, 0].legend(frameon=False, fontsize=8, ncol=2)
    axes[1, 1].legend(frameon=False, fontsize=8, ncol=2)
    figure.suptitle("3DGS Prague-share matrix: 5 ms DualPI2 step threshold", fontsize=15)
    figure.text(
        0.5, 0.012,
        "One repetition per cell. Goodput is received application payload; RTT is transport-smoothed RTT, not qdisc sojourn time.",
        ha="center", fontsize=9,
    )
    figure.tight_layout(rect=(0.02, 0.04, 0.99, 0.95))
    paths = [output_dir / "matrix-summary.svg", output_dir / "matrix-summary.png"]
    figure.savefig(paths[0])
    figure.savefig(paths[1], dpi=180)
    plt.close(figure)
    return paths


def _render_timeseries(
    roots: dict[str, Path], fractions: list[float], output_dir: Path, *, bin_us: int,
    deadline_s: float,
) -> list[Path]:
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(
        len(fractions), 4, figsize=(19.0, 3.15 * len(fractions)), squeeze=False
    )
    for row_index, fraction in enumerate(fractions):
        case = f"l4s-{_tag(fraction)}-rep-01"
        for mode_index, (mode, label, _) in enumerate(RUNS):
            rate_axis = axes[row_index, mode_index * 2]
            rtt_axis = axes[row_index, mode_index * 2 + 1]
            total_rates: list[float] = []
            case_root = _cell_root(roots[mode], fraction) / case
            combined_timeline = case_root / "combined-arrival-timeline.csv"
            case_result = json.loads((case_root / "result.json").read_text(encoding="utf-8"))
            for stream, stream_label, color in STREAMS:
                stream_root = case_root / stream
                rates = _experiment_goodput(combined_timeline, stream, bin_us=bin_us)
                if len(total_rates) < len(rates):
                    total_rates.extend([0.0] * (len(rates) - len(total_rates)))
                for index, value in enumerate(rates):
                    total_rates[index] += value
                if rates:
                    edges = [index * bin_us / 1_000_000.0 for index in range(len(rates) + 1)]
                    rate_axis.stairs(rates, edges, color=color, linewidth=1.25, label=stream_label)
                # An endpoint's empty path still exchanges handshake/control
                # packets, but that RTT is not representative of a media flow.
                if rates:
                    rtt = _measured_rtt(
                        stream_root / "transport-metrics.csv",
                        stream_root / "publisher-result.json",
                        int(case_result["workload_start_epoch_us"]),
                        deadline_s,
                    )
                    rtt_axis.plot(
                        [point[0] for point in rtt], [point[1] for point in rtt],
                        color=color, linewidth=1.0, label=stream_label,
                    )
            if total_rates:
                edges = [index * bin_us / 1_000_000.0 for index in range(len(total_rates) + 1)]
                rate_axis.stairs(total_rates, edges, color="#212121", linewidth=1.45, label="Total")
            rate_axis.set_title(f"{label}: payload goodput")
            rtt_axis.set_title(f"{label}: smoothed RTT")
            rate_axis.set_ylabel(f"{fraction:g} share\nMbit/s")
            rtt_axis.set_ylabel("ms")
            for axis in (rate_axis, rtt_axis):
                axis.set_xlim(0, deadline_s)
                axis.set_ylim(bottom=0)
                axis.grid(True, alpha=0.22)
                if row_index == len(fractions) - 1:
                    axis.set_xlabel("Time since workload start (s)")
    axes[0, 0].legend(frameon=False, ncol=3, fontsize=8)
    axes[0, 1].legend(frameon=False, ncol=2, fontsize=8)
    figure.suptitle("Foreground throughput and RTT throughout each matrix cell", fontsize=15)
    figure.text(
        0.5, 0.008,
        "Rows are requested Prague payload shares. Empty endpoint streams remain connected but carry zero application payload.",
        ha="center", fontsize=9,
    )
    figure.tight_layout(rect=(0.02, 0.025, 0.995, 0.97))
    paths = [output_dir / "matrix-timeseries.svg", output_dir / "matrix-timeseries.png"]
    figure.savefig(paths[0])
    figure.savefig(paths[1], dpi=160)
    plt.close(figure)
    return paths


def _render_availability(
    root: Path, fractions: list[float], output_dir: Path, *, deadline_s: float,
) -> list[Path]:
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(len(fractions), 1, figsize=(11, 2.4 * len(fractions)), sharex=True)
    if len(fractions) == 1:
        axes = [axes]
    for axis, fraction in zip(axes, fractions):
        case_root = _cell_root(root, fraction)
        input_root = case_root / "inputs" / f"l4s-{_tag(fraction)}"
        for name, color, label in (("high-prague", "#ef6c00", "Prague"), ("low-reno", "#1565c0", "Classic")):
            schedule_path = input_root / f"{name}-release-ms.txt"
            if not schedule_path.is_file():
                continue
            times = sorted(
                int(line.split()[0]) / 1000.0
                for line in schedule_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            axis.step(times, range(1, len(times) + 1), where="post", color=color, label=label)
        axis.set_ylabel(f"{fraction:g}\nobjects")
        axis.grid(True, alpha=0.25)
        axis.legend(frameon=False, loc="upper left", ncol=2)
        axis.set_xlim(0, deadline_s)
    axes[-1].set_xlabel("Release time since workload start (s)")
    figure.suptitle("3DGS object availability by frozen visibility schedule")
    figure.tight_layout()
    paths = [output_dir / "object-availability-timeline.svg", output_dir / "object-availability-timeline.png"]
    figure.savefig(paths[0])
    figure.savefig(paths[1], dpi=180)
    plt.close(figure)
    return paths


def render_matrix(
    dualpi2_root: Path,
    classic_root: Path,
    output_dir: Path,
    *,
    fractions: list[float] | None = None,
    deadline_s: float = 45.0,
    bin_us: int = 1_000_000,
) -> dict:
    fractions = fractions or [0.0, 0.25, 0.5, 0.75, 1.0]
    roots = {"dualpi2": dualpi2_root, "classic": classic_root}
    configurations = {
        mode: _matrix_configuration(root, fractions) for mode, root in roots.items()
    }
    differing = {
        key: [configurations["dualpi2"].get(key), configurations["classic"].get(key)]
        for key in sorted(set(configurations["dualpi2"]) | set(configurations["classic"]))
        if configurations["dualpi2"].get(key) != configurations["classic"].get(key)
    }
    if differing != {"downstream_mode": ["dualpi2", "classic"]}:
        raise ValueError(f"runs differ beyond downstream mode: {differing}")

    analyses = {mode: _analysis_cases(root) for mode, root in roots.items()}
    rows: list[dict] = []
    sources: list[dict] = []
    for mode, _, _ in RUNS:
        for fraction in fractions:
            case_name = f"l4s-{_tag(fraction)}-rep-01"
            case_root = _cell_root(roots[mode], fraction) / case_name
            result = json.loads((case_root / "result.json").read_text(encoding="utf-8"))
            analysis = analyses[mode][fraction]
            mean_overtaken, total_overtaken = overtaking_metrics(analysis)
            row = {
                "downstream_mode": mode,
                "case": case_name,
                "requested_prague_share": fraction,
                "actual_prague_share": result["actual_l4s_byte_fraction"],
                "acceptance": analysis["acceptance"]["status"],
                "matched_l4s_packets": analysis["capture_counts"]["prague_provider_matched_packets"],
                "overtaking_l4s_packets": analysis["provider_reordering"]["prague_packets_with_overtake"],
                "mean_overtaken_bytes_per_l4s_packet": mean_overtaken,
                "total_overtaken_byte_pairs": total_overtaken,
            }
            for stream, _, _ in STREAMS:
                path_result = result["paths"][stream]
                metrics_path = case_root / stream / "transport-metrics.csv"
                received_payload_bytes = int(path_result["received_payload_bytes"])
                row[f"{stream}_received_payload_bytes"] = received_payload_bytes
                row[f"{stream}_mean_goodput_mbps"] = (
                    float(received_payload_bytes) * 8.0 / deadline_s / 1_000_000.0
                )
                row[f"{stream}_mean_rtt_ms"] = None
                if received_payload_bytes:
                    case_result = result
                    row[f"{stream}_mean_rtt_ms"] = statistics.fmean(
                        value for _, value in _measured_rtt(
                            metrics_path,
                            case_root / stream / "publisher-result.json",
                            int(case_result["workload_start_epoch_us"]),
                            deadline_s,
                        )
                    )
                arrival_path = case_root / stream / "arrival-timeline.csv"
                sources.append({
                    "run": roots[mode].name,
                    "case": case_name,
                    "stream": stream,
                    "arrival_timeline": str(arrival_path),
                    "arrival_timeline_sha256": sha256(arrival_path),
                    "transport_metrics": str(metrics_path),
                    "transport_metrics_sha256": sha256(metrics_path),
                })
            rows.append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "matrix-summary.csv"
    _write_summary(rows, summary_csv)
    outputs = _render_summary(rows, output_dir) + _render_timeseries(
        roots, fractions, output_dir, bin_us=bin_us, deadline_s=deadline_s
    )
    outputs += _render_availability(roots["dualpi2"], fractions, output_dir, deadline_s=deadline_s)
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "comparison": "DualPI2 versus Classic downstream across Prague shares",
        "controlled_configuration_difference": differing,
        "fractions": fractions,
        "definitions": {
            "mean_overtaken_bytes_per_l4s_packet": "sum of overtaken Classic payload byte-pairs divided by all matched Prague packets; zero-overtake packets included",
            "total_overtaken_byte_pairs": "sum of Classic payload bytes overtaken by each Prague packet; a Classic byte may contribute more than once",
            "mean_goodput": "received application payload bytes divided by the configured workload deadline",
            "timeseries_goodput": "received application payload bytes in fixed one-second bins",
            "rtt": "transport-reported smoothed RTT; not direct qdisc sojourn time",
            "repetitions": 1,
        },
        "outputs": [str(summary_csv), *(str(path) for path in outputs)],
        "sources": sources,
    }
    manifest_path = output_dir / "matrix-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dualpi2_root", type=Path)
    parser.add_argument("classic_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--deadline-s",
        type=float,
        default=45.0,
        help="measured workload horizon used for axes and mean goodput (default: 45)",
    )
    args = parser.parse_args()
    render_matrix(args.dualpi2_root, args.classic_root, args.output, deadline_s=args.deadline_s)


if __name__ == "__main__":
    main()
