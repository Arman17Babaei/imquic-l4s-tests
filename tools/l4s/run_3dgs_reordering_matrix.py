#!/usr/bin/env python3
"""Run each 3DGS Prague-share cell in a fresh Mininet process."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/l4s/run_3dgs_reordering.py"
ANALYZER = ROOT / "tools/l4s/analyze_3dgs_reordering.py"


def fractions(value: str) -> list[float]:
    try:
        parsed = [float(item) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError("invalid Prague-share list") from error
    if not parsed or any(value < 0.0 or value > 1.0 for value in parsed):
        raise argparse.ArgumentTypeError("Prague shares must be in [0,1]")
    return parsed


def tag(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".").replace(".", "p")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--frozen-demand", type=Path, required=True)
    parser.add_argument("--l4s-fractions", type=fractions, required=True)
    args, forwarded = parser.parse_known_args()
    args.output.mkdir(parents=True, exist_ok=False)

    summaries = []
    analyses = []
    cells = []
    for fraction in args.l4s_fractions:
        cell = args.output / f"fraction-{tag(fraction)}"
        command = [
            sys.executable,
            str(RUNNER),
            "--output", str(cell),
            "--source-bundle", str(args.source_bundle),
            "--frozen-demand", str(args.frozen_demand),
            "--l4s-fractions", f"{fraction:g}",
            *forwarded,
        ]
        subprocess.run(command, cwd=ROOT, check=True)
        subprocess.run([sys.executable, str(ANALYZER), str(cell)], cwd=ROOT, check=True)
        summary = json.loads((cell / "summary.json").read_text(encoding="utf-8"))
        analysis = json.loads(
            (cell / "reordering-analysis.json").read_text(encoding="utf-8")
        )
        summaries.extend(summary["cases"])
        analyses.extend(analysis["cases"])
        cells.append({
            "requested_l4s_fraction": fraction,
            "root": str(cell),
            "summary": str(cell / "summary.json"),
            "analysis": str(cell / "reordering-analysis.json"),
        })

    (args.output / "summary.json").write_text(
        json.dumps({"cases": summaries}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output / "reordering-analysis.json").write_text(
        json.dumps({"cases": analyses}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output / "matrix.json").write_text(
        json.dumps({"cell_isolation": "fresh runner and Mininet process", "cells": cells},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
