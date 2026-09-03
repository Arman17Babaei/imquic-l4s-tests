#!/usr/bin/env python3
"""Plot stored IMQUIC results across BBR and BBRv2 background controllers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
import numpy as np

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ModuleNotFoundError as error:
    raise SystemExit(
        "Persian shaping dependencies are required: pip install arabic-reshaper python-bidi"
    ) from error


INK = "#28323c"
GRID = "#d8dde3"
BACKGROUND_CONTROLLERS = (
    ("bbr", "BBR", "#1976a3", "//"),
    ("bbr2", "BBRv2", "#d17824", ".."),
)
IMQUIC_MODES = (
    ("l4s-off", "Reno / Not-ECT"),
    ("l4s-ect0", "Reno / ECT(0)"),
    ("l4s-on", "Prague / ECT(1)"),
)


def fa(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def configure(font: Path) -> None:
    font_manager.fontManager.addfont(str(font))
    name = font_manager.FontProperties(fname=str(font)).get_name()
    matplotlib.rcParams.update({
        "font.family": name,
        "font.size": 10.5,
        "axes.edgecolor": INK,
        "axes.labelcolor": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "text.color": INK,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def read_run(root: Path, expected_controller: str) -> tuple[dict, list[dict], Path, Path]:
    benchmark_path = root / "benchmark.json"
    analysis_path = root / "analysis.json"
    benchmark_record = json.loads(benchmark_path.read_text(encoding="utf-8"))
    benchmark = benchmark_record.get("configuration", benchmark_record)
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    if benchmark.get("background_congestion") != expected_controller:
        raise ValueError(f"{root}: unexpected background controller")
    if analysis.get("background_congestion") != expected_controller:
        raise ValueError(f"{root}: analysis controller mismatch")
    rows = analysis.get("rows", [])
    if not rows:
        raise ValueError(f"{root}: no measured rows")
    return benchmark, rows, benchmark_path, analysis_path


def compatibility_gate(configurations: dict[str, dict]) -> dict:
    expected = {
        "background_rates_mbps": [20],
        "background_warmup_seconds": 2,
        "bottleneck_mbps": 20.0,
        "foreground_seconds": 8,
        "repetitions": 3,
        "topology": "client--s1(OVSBridge+HTB+DualPI2)--server",
        "transfer_bytes": 67108864,
    }
    mismatches = []
    for controller, configuration in configurations.items():
        for key, value in expected.items():
            if configuration.get(key) != value:
                mismatches.append({"controller": controller, "field": key,
                                   "actual": configuration.get(key), "expected": value})
    if mismatches:
        raise ValueError(f"incompatible recordings: {mismatches}")
    return expected


def grouped_rows(rows: list[dict]) -> dict[str, list[dict]]:
    grouped = {mode: [] for mode, _label in IMQUIC_MODES}
    for row in rows:
        mode = row.get("mode")
        if mode not in grouped or row.get("background_target_mbps") != 20:
            continue
        grouped[mode].append(row)
    missing = [mode for mode, values in grouped.items() if len(values) != 3]
    if missing:
        raise ValueError(f"expected three repetitions per mode, missing: {missing}")
    return grouped


def ranges(values: list[float]) -> tuple[float, float, float]:
    mean = float(np.mean(values))
    return mean, mean - min(values), max(values) - mean


def bar_panel(axis: plt.Axes, data: dict[str, dict[str, list[dict]]], field: str,
              ylabel: str) -> None:
    positions = np.arange(len(IMQUIC_MODES))
    width = 0.35
    for index, (controller, display, color, hatch) in enumerate(BACKGROUND_CONTROLLERS):
        offset = (index - 0.5) * width
        means, lower, upper = [], [], []
        for mode, _label in IMQUIC_MODES:
            values = [float(row[field]) for row in data[controller][mode]]
            mean, low, high = ranges(values)
            means.append(mean)
            lower.append(low)
            upper.append(high)
        bars = axis.bar(positions + offset, means, width=width, color=color,
                       edgecolor=INK, linewidth=.55, hatch=hatch,
                       label=display, zorder=3)
        axis.errorbar(positions + offset, means, yerr=[lower, upper], fmt="none",
                      ecolor=INK, capsize=3, linewidth=1, zorder=4)
        for bar, value in zip(bars, means):
            axis.text(bar.get_x() + bar.get_width() / 2, value, f"{value:.1f}",
                      ha="center", va="bottom", fontsize=8.3, color=INK)
    axis.set_xticks(positions, [label for _mode, label in IMQUIC_MODES], fontsize=9)
    axis.set_ylabel(ylabel)
    axis.set_ylim(bottom=0)
    axis.grid(axis="y", color=GRID, linewidth=.65, zorder=0)
    axis.set_axisbelow(True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbr-root", type=Path, required=True)
    parser.add_argument("--bbr2-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=Path("fonts/XB Niloofar.ttf"))
    args = parser.parse_args()
    configure(args.font)

    roots = {"bbr": args.bbr_root, "bbr2": args.bbr2_root}
    configurations: dict[str, dict] = {}
    grouped: dict[str, dict[str, list[dict]]] = {}
    sources = []
    for controller, root in roots.items():
        configuration, rows, benchmark_path, analysis_path = read_run(root, controller)
        configurations[controller] = configuration
        grouped[controller] = grouped_rows(rows)
        sources.append({
            "background_controller": controller,
            "benchmark": str(benchmark_path.resolve()),
            "benchmark_sha256": sha256(benchmark_path),
            "analysis": str(analysis_path.resolve()),
            "analysis_sha256": sha256(analysis_path),
        })
    invariants = compatibility_gate(configurations)

    figure, axes = plt.subplots(1, 2, figsize=(11.1, 4.25))
    bar_panel(axes[0], grouped, "quic_goodput_mbps", fa("گودپوت برنامهٔ IMQUIC (مگابیت‌برثانیه)"))
    bar_panel(axes[1], grouped, "final_rtt_us", fa("RTT هموار در پایان انتقال (میکروثانیه)"))
    axes[1].set_ylim(0, 22000)
    handles, labels = axes[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper center", ncol=2, frameon=False,
                  bbox_to_anchor=(.5, 1.03), fontsize=10)
    figure.subplots_adjust(left=.085, right=.99, bottom=.19, top=.84, wspace=.28)

    args.output_root.mkdir(parents=True, exist_ok=True)
    stem = "imquic-background-congestion-control-fa"
    for suffix in ("pdf", "svg", "png"):
        figure.savefig(args.output_root / f"{stem}.{suffix}", bbox_inches="tight",
                       dpi=240 if suffix == "png" else None)
    plt.close(figure)

    summary = {
        "schema_version": 1,
        "claim_type": "three_repetition_descriptive_comparison",
        "comparison": "IMQUIC foreground controllers under BBR versus BBRv2 TCP background",
        "compatibility_gate": {"passed": True, "invariants": invariants},
        "definitions": {
            "imquic_goodput_mbps": "application goodput reported by the IMQUIC fixture",
            "final_rtt_us": "transport-reported smoothed RTT sample at the end of transfer",
            "error_bars": "minimum-to-maximum range across three repetitions",
        },
        "sources": sources,
        "rows": {
            controller: {mode: [{key: row[key] for key in (
                "repetition", "quic_goodput_mbps", "final_rtt_us", "background_actual_mbps",
                "background_wire_mbps", "quic_wire_mbps")}
                for row in grouped[controller][mode]]
                for mode, _label in IMQUIC_MODES}
            for controller, _display, _color, _hatch in BACKGROUND_CONTROLLERS
        },
    }
    summary_path = args.output_root / f"{stem}-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "artifacts": {
            path.name: sha256(path)
            for path in [
                args.output_root / f"{stem}.pdf",
                args.output_root / f"{stem}.svg",
                args.output_root / f"{stem}.png",
                summary_path,
            ]
        },
        "verification": {
            "provenance_compatibility_gate_passed": True,
            "three_repetitions_per_cell": True,
            "background_controllers": ["bbr", "bbr2"],
            "imquic_modes": [mode for mode, _label in IMQUIC_MODES],
        },
    }
    (args.output_root / f"{stem}-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
