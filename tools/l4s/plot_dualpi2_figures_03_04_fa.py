#!/usr/bin/env python3
"""Render the Persian or English DualPI2 topology for Figures 3--4."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt

try:
    from thesis_topology import draw_dualpi2_validation_topology
    from thesis_localization import FigureLocalizer
except ModuleNotFoundError:
    from tools.l4s.thesis_topology import draw_dualpi2_validation_topology
    from tools.l4s.thesis_localization import FigureLocalizer


TEXT = FigureLocalizer("fa")


def fa(text: str) -> str:
    return TEXT(text)


def configure(font: Path) -> None:
    font_manager.fontManager.addfont(str(font))
    matplotlib.rcParams.update({
        "font.family": font_manager.FontProperties(fname=str(font)).get_name(),
        "font.size": 11,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
    })


def main() -> None:
    global TEXT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path("results/figures/thesis-figures-03-04"))
    parser.add_argument("--font", type=Path, default=Path("fonts/XB Niloofar.ttf"))
    parser.add_argument("--language", choices=("fa", "en"), default="fa")
    args = parser.parse_args()
    TEXT = FigureLocalizer(args.language)
    if not args.font.is_file():
        raise SystemExit(f"missing figure font: {args.font}")
    args.output.mkdir(parents=True, exist_ok=True)
    configure(args.font)
    figure, axis = plt.subplots(figsize=(9.4, 2.8))
    draw_dualpi2_validation_topology(axis, fa)
    for suffix in ("pdf", "svg", "png"):
        stem = TEXT.stem("topology-for-figures-03-04-fa")
        figure.savefig(args.output / f"{stem}.{suffix}",
                       bbox_inches="tight", dpi=220 if suffix == "png" else None)
    plt.close(figure)


if __name__ == "__main__":
    main()
