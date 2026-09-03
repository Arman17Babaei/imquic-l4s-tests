#!/usr/bin/env python3
"""Run one matched full-Classic/full-L4S 3DGS pair in fresh Mininet cells."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/l4s/run_3dgs_reordering.py"
ANALYZER = ROOT / "tools/l4s/analyze_3dgs_reordering.py"
PAIR_ANALYZER = ROOT / "tools/l4s/thesis_3dgs_pair.py"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--frozen-demand", type=Path, required=True)
    args, common = parser.parse_known_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cells = []
    for label, fraction, downstream in (
        ("classic", "0", "classic"),
        ("l4s", "1", "dualpi2"),
    ):
        cell = args.output / label
        command = [
            sys.executable, str(RUNNER), "--output", str(cell),
            "--source-bundle", str(args.source_bundle),
            "--frozen-demand", str(args.frozen_demand),
            "--l4s-fractions", fraction, "--repetitions", "1",
            "--downstream-mode", downstream, *common,
        ]
        subprocess.run(command, cwd=ROOT, check=True)
        subprocess.run([sys.executable, str(ANALYZER), str(cell)], cwd=ROOT,
                       check=True)
        cells.append({"label": label, "fraction": float(fraction),
                      "downstream_mode": downstream, "root": str(cell)})
    subprocess.run([
        sys.executable, str(PAIR_ANALYZER), str(args.output / "classic"),
        str(args.output / "l4s"), "--gate",
    ], cwd=ROOT, check=True)
    (args.output / "pair-manifest.json").write_text(
        json.dumps({"execution_order": ["classic", "l4s"], "cells": cells},
                   indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
