#!/usr/bin/env python3
"""Plot RTT and second-bottleneck bandwidth for the stored quality comparison."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

try:
    from .plot_quality_through_run_fa import (
        CASES,
        COLORS,
        GRID,
        STYLES,
        compatibility_gate,
        configure,
        fa,
        label,
        provenance_for,
        read_csv,
        save_figure,
        sha256,
    )
except ImportError:
    from plot_quality_through_run_fa import (
        CASES,
        COLORS,
        GRID,
        STYLES,
        compatibility_gate,
        configure,
        fa,
        label,
        provenance_for,
        read_csv,
        save_figure,
        sha256,
    )


HORIZON_S = 11.12
RTT_BIN_S = 0.1
BANDWIDTH_BIN_S = 0.5


def binned_median(rows: list[dict[str, str]], field: str) -> tuple[np.ndarray, np.ndarray]:
    bins = np.arange(0.0, HORIZON_S + RTT_BIN_S, RTT_BIN_S)
    values: list[list[float]] = [[] for _ in range(len(bins) - 1)]
    for row in rows:
        time_s = float(row["time_us"]) / 1_000_000.0
        if not 0 <= time_s <= HORIZON_S:
            continue
        index = min(int(time_s / RTT_BIN_S), len(values) - 1)
        values[index].append(float(row[field]) / 1000.0)
    centers = bins[:-1] + RTT_BIN_S / 2.0
    medians = np.array([np.median(group) if group else np.nan for group in values])
    return centers, medians


def wire_bandwidth(rows: list[dict[str, str]], origin_s: float,
                   media_ports: set[int]) -> tuple[np.ndarray, np.ndarray]:
    complete_horizon = np.floor(HORIZON_S / BANDWIDTH_BIN_S) * BANDWIDTH_BIN_S
    bins = np.arange(0.0, complete_horizon + BANDWIDTH_BIN_S / 2.0, BANDWIDTH_BIN_S)
    payload = np.zeros(len(bins) - 1, dtype=float)
    for row in rows:
        time_s = float(row["time_s"]) - origin_s
        if not 0 <= time_s <= HORIZON_S or int(row["source_port"]) not in media_ports:
            continue
        index = min(int(time_s / BANDWIDTH_BIN_S), len(payload) - 1)
        payload[index] += int(row["payload_bytes"])
    centers = bins[:-1] + BANDWIDTH_BIN_S / 2.0
    return centers, payload * 8.0 / BANDWIDTH_BIN_S / 1_000_000.0


def finite_summary(values: np.ndarray) -> dict:
    finite = values[np.isfinite(values)]
    return {
        "samples": int(len(finite)),
        "median": float(np.median(finite)) if len(finite) else None,
        "p95": float(np.percentile(finite, 95)) if len(finite) else None,
        "mean": float(np.mean(finite)) if len(finite) else None,
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--font", type=Path, default=Path("fonts/XB Niloofar.ttf"))
    args = parser.parse_args()
    output = args.output_root or args.comparison_root / "quality-plots"
    output.mkdir(parents=True, exist_ok=True)
    configure(args.font)

    configurations = []
    cases = []
    sources = []
    for name, mode, fraction in CASES:
        case = args.comparison_root / name
        provenance_path, configuration = provenance_for(case)
        configurations.append((name, mode, fraction, configuration))
        result_path = case / "result.json"
        result = json.loads(result_path.read_text())
        prague_path = case / "high-prague/transport-metrics.csv"
        reno_path = case / "low-reno/transport-metrics.csv"
        packet_path = case / "downstream_egress.packet-order.csv"
        required = (result_path, prague_path, reno_path, packet_path)
        if not all(path.is_file() for path in required):
            raise ValueError(f"missing network evidence for {name}")
        prague = read_csv(prague_path)
        reno = read_csv(reno_path)
        packets = read_csv(packet_path)
        origin_s = float(result["workload_start_epoch_us"]) / 1_000_000.0
        media_ports = {int(item["port"]) for item in result["paths"].values()}
        bandwidth_time, bandwidth_mbps = wire_bandwidth(packets, origin_s, media_ports)
        cases.append({
            "name": name,
            "mode": mode,
            "fraction": fraction,
            "prague": prague,
            "reno": reno,
            "bandwidth_time": bandwidth_time,
            "bandwidth_mbps": bandwidth_mbps,
        })
        sources.append({
            "case": name,
            "result_sha256": sha256(result_path),
            "prague_transport_sha256": sha256(prague_path),
            "reno_transport_sha256": sha256(reno_path),
            "downstream_packet_order_sha256": sha256(packet_path),
        })
    invariants = compatibility_gate(configurations)

    fig, axes = plt.subplots(2, 1, figsize=(8.5, 6.2), sharex=True)
    rtt_summary = []
    for case in cases:
        time_s, rtt_ms = binned_median(case["prague"], "rtt_us")
        axes[0].plot(time_s, rtt_ms, color=COLORS[case["mode"]],
                     linestyle=STYLES[case["fraction"]], linewidth=1.7,
                     label=label(case["mode"], case["fraction"]))
        rtt_summary.append({"case": case["name"], "path": "prague",
                            **finite_summary(rtt_ms)})
        if case["fraction"] == 0.25:
            time_s, rtt_ms = binned_median(case["reno"], "rtt_us")
            axes[1].plot(time_s, rtt_ms, color=COLORS[case["mode"]],
                         linestyle=STYLES[case["fraction"]], linewidth=1.7,
                         label=label(case["mode"], case["fraction"]))
            rtt_summary.append({"case": case["name"], "path": "reno",
                                **finite_summary(rtt_ms)})
    axes[0].set_ylabel("RTT Prague (ms)")
    axes[1].set_ylabel("RTT Reno (ms)")
    axes[1].set_xlabel(fa("زمان مسیر دوربین (ثانیه)"))
    axes[0].legend(frameon=False, fontsize=8, ncol=2, loc="lower center",
                   bbox_to_anchor=(.5, 1.01))
    axes[1].legend(frameon=False, fontsize=8, ncol=2, loc="lower center",
                   bbox_to_anchor=(.5, 1.01))
    for axis in axes:
        axis.axhline(20.0, color="#777777", linestyle=":", linewidth=1.0,
                     label=fa("RTT پایه"))
        axis.set_xlim(0, HORIZON_S)
        axis.grid(True, color=GRID, linewidth=.65)
        axis.set_axisbelow(True)
    axes[1].text(.985, .055, fa("میانه بازه‌های ۱۰۰ میلی‌ثانیه‌ای"),
                 transform=axes[1].transAxes, ha="right", fontsize=8.5,
                 color="#8b3a3a", bbox={"facecolor": "white", "edgecolor": "none",
                                         "alpha": .82, "pad": 1.5})
    fig.subplots_adjust(hspace=.22)
    save_figure(fig, output, "rtt-through-run-fa")

    fig, axis = plt.subplots(figsize=(8.5, 4.2))
    bandwidth_summary = []
    bandwidth_rows = []
    for case in cases:
        axis.plot(case["bandwidth_time"], case["bandwidth_mbps"],
                  color=COLORS[case["mode"]], linestyle=STYLES[case["fraction"]],
                  linewidth=1.8, label=label(case["mode"], case["fraction"]))
        bandwidth_summary.append({"case": case["name"],
                                  **finite_summary(case["bandwidth_mbps"])})
        for time_s, rate in zip(case["bandwidth_time"], case["bandwidth_mbps"]):
            bandwidth_rows.append({"case": case["name"], "time_s": time_s,
                                   "wire_udp_payload_mbps": rate})
    axis.axhline(50.0, color="#777777", linestyle=":", linewidth=1.0,
                 label=fa("ظرفیت گلوگاه دوم"))
    axis.set_xlim(0, HORIZON_S)
    axis.set_ylim(bottom=0)
    axis.set_xlabel(fa("زمان مسیر دوربین (ثانیه)"))
    axis.set_ylabel("UDP payload (Mbit/s)  " + fa("نرخ خروجی"))
    axis.legend(frameon=False, fontsize=8, ncol=2, loc="lower right")
    axis.grid(True, color=GRID, linewidth=.65)
    axis.set_axisbelow(True)
    axis.text(.02, .06, fa("مجموع بار UDP در بازه‌های کامل ۵۰۰ میلی‌ثانیه‌ای"),
              transform=axis.transAxes, ha="left", fontsize=8.5, color="#8b3a3a",
              bbox={"facecolor": "white", "edgecolor": "none", "alpha": .82,
                    "pad": 1.5})
    save_figure(fig, output, "bandwidth-through-run-fa")

    write_csv(output / "bandwidth-through-run-fa.csv", bandwidth_rows)
    summary = {
        "schema_version": 1,
        "claim_type": "single_repetition_descriptive_transport_measurement",
        "horizon_s": HORIZON_S,
        "rtt": {
            "source": "sender transport rtt_us",
            "aggregation": "median in 100 ms bins",
            "cases": rtt_summary,
        },
        "bandwidth": {
            "source": "UDP payload bytes captured at second-bottleneck egress",
            "aggregation": "sum in complete 500 ms bins, converted to Mbit/s",
            "scope": "media source ports 4443 and 4444; includes QUIC overhead and retransmissions",
            "not_application_goodput": True,
            "cases": bandwidth_summary,
        },
        "compatibility_gate": {"passed": True, "invariants": invariants},
        "sources": sources,
    }
    summary_path = output / "network-through-run-fa-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    artifacts = {}
    for stem in ("rtt-through-run-fa", "bandwidth-through-run-fa"):
        for suffix in ("pdf", "svg", "png"):
            path = output / f"{stem}.{suffix}"
            artifacts[path.name] = sha256(path)
    for path in (output / "bandwidth-through-run-fa.csv", summary_path):
        artifacts[path.name] = sha256(path)
    (output / "network-through-run-fa-manifest.json").write_text(json.dumps({
        "artifacts": artifacts,
        "verification": {
            "provenance_gate_passed": True,
            "shared_horizon_s": HORIZON_S,
            "rtt_bin_ms": RTT_BIN_S * 1000,
            "bandwidth_bin_ms": BANDWIDTH_BIN_S * 1000,
            "persian_shaping": True,
        },
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
