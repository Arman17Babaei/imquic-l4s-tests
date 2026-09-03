#!/usr/bin/env python3
"""Render a Persian MoQ/TCP congestion-control matrix from a promoted record."""

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
GRID = "#ffffff"
MODES = (
    ("reno", "Reno / Not-ECT"),
    ("bbr", "picoquic BBR / Not-ECT"),
    ("prague", "Prague / ECT(1)"),
)
BACKGROUNDS = (
    ("cubic", "Cubic"),
    ("reno", "Reno"),
    ("bbr", "BBR"),
    ("bbr2", "BBRv2"),
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
    })


def load_matrix(summary_path: Path) -> tuple[np.ndarray, list[dict]]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    cases = summary.get("cases", [])
    if len(cases) != len(MODES) * len(BACKGROUNDS):
        raise ValueError(f"expected 12 cases, got {len(cases)}")
    by_key = {(case["mode"], case["background_congestion"]): case for case in cases}
    expected = {(mode, background) for mode, _label in MODES
                for background, _background_label in BACKGROUNDS}
    if set(by_key) != expected:
        raise ValueError("record does not contain the complete controller matrix")
    for key, case in by_key.items():
        if case.get("repetition") != 1 or case.get("bins") != 8:
            raise ValueError(f"{key}: expected one 8-second observation")
        if not 98.0 <= float(case["bottleneck_utilization_percent_mean"]) <= 100.0:
            raise ValueError(f"{key}: unexpected bottleneck utilization")
    share_percent = np.array([
        [100.0 * by_key[(mode, background)]["foreground_share_mean"]
         for background, _label in BACKGROUNDS]
        for mode, _label in MODES
    ], dtype=float)
    return share_percent, cases


def draw_heatmap(axis: plt.Axes, values: np.ndarray, *, cmap: str, value_format: str,
                 colorbar_label: str, title: str, figure: plt.Figure,
                 value_limits: tuple[float, float]) -> None:
    image = axis.pcolormesh(
        np.arange(len(BACKGROUNDS) + 1), np.arange(len(MODES) + 1), values,
        cmap=cmap, vmin=value_limits[0], vmax=value_limits[1], shading="flat",
        edgecolors=GRID, linewidth=2.1,
    )
    axis.set_xlim(0, len(BACKGROUNDS))
    axis.set_ylim(len(MODES), 0)
    axis.set_xticks(np.arange(len(BACKGROUNDS)) + .5,
                     [label for _mode, label in BACKGROUNDS])
    axis.set_yticks(np.arange(len(MODES)) + .5, [label for _mode, label in MODES])
    axis.set_xlabel(fa("کنترل ازدحام TCP پس‌زمینه"))
    axis.set_title(fa(title), fontsize=11.5, pad=10)
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            color = "white" if values[row, column] > np.mean(value_limits) else INK
            axis.text(column + .5, row + .5, value_format.format(values[row, column]),
                      ha="center", va="center", color=color, fontsize=10.5,
                      fontweight="semibold")
    colorbar = figure.colorbar(image, ax=axis, fraction=.048, pad=.04)
    colorbar.outline.set_edgecolor(INK)
    colorbar.set_label(colorbar_label, rotation=90, labelpad=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=Path("fonts/XB Niloofar.ttf"))
    args = parser.parse_args()
    configure(args.font)

    summary_path = args.record_root / "summary.json"
    result_path = args.record_root / "result.json"
    share_percent, cases = load_matrix(summary_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("conclusion") != "supports":
        raise ValueError("record is not a supporting result")

    figure, axis = plt.subplots(figsize=(6.35, 4.35))
    draw_heatmap(
        axis, share_percent, cmap="YlGnBu", value_format="{:.1f}%",
        colorbar_label=fa("درصد"),
        title="سهم جریان MoQ از ترافیک گلوگاه",
        figure=figure, value_limits=(0, 100),
    )
    axis.set_ylabel(fa("کنترل ازدحام IMQUIC"))
    figure.subplots_adjust(left=.26, right=.94, bottom=.20, top=.86)

    args.output_root.mkdir(parents=True, exist_ok=True)
    stem = "figure-moq-background-cc-matrix-fa"
    for suffix in ("pdf", "svg", "png"):
        figure.savefig(args.output_root / f"{stem}.{suffix}", bbox_inches="tight",
                       dpi=240 if suffix == "png" else None)
    plt.close(figure)

    artifact_paths = [
        args.output_root / f"{stem}.pdf",
        args.output_root / f"{stem}.svg",
        args.output_root / f"{stem}.png",
    ]
    report = {
        "schema_version": 1,
        "source_record": str(args.record_root.resolve()),
        "source_summary_sha256": sha256(summary_path),
        "source_result_sha256": sha256(result_path),
        "matrix": {
            "imquic_modes": [mode for mode, _label in MODES],
            "background_controllers": [background for background, _label in BACKGROUNDS],
            "cells": len(cases),
            "repetitions_per_cell": 1,
            "duration_seconds": 8,
            "bottleneck_mbps": 20,
        },
        "definitions": {
            "share": "foreground_wire_mbps_mean divided by combined_wire_mbps_mean",
        },
    }
    report_path = args.output_root / f"{stem}-summary.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    artifacts = {path.name: sha256(path) for path in [*artifact_paths, report_path]}
    (args.output_root / f"{stem}-manifest.json").write_text(
        json.dumps({
            "artifacts": artifacts,
            "verification": {
                "complete_3_by_4_controller_matrix": True,
                "source_record_supports_claim": True,
                "single_eight_second_observation_per_cell": True,
            },
        }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
