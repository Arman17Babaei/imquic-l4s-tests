#!/usr/bin/env python3
"""Run the controlled backlog matrix used by Figures 13--14."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    from .three_dgs_bundle import bundle_summary
except ImportError:
    from three_dgs_bundle import bundle_summary

ROOT = Path(__file__).resolve().parents[2]
BDP_BYTES = 100_000_000 / 8 * 0.020
BACKLOG_GRID = (0.0, 0.5, 1.0, 2.0, 4.0)
LEAD_GRID = (0.0, 0.5, 1.0, 2.0, 4.0)


def prepare(source: Path, out: Path, backlog: float, lead: float) -> Path:
    command = [sys.executable, str(ROOT / "tools/l4s/prepare_backlog_probe.py"),
               "--source-bundle", str(source), "--backlog-bytes", str(round(backlog * BDP_BYTES)),
               "--lead-ms", str(lead * 20.0), "--output", str(out)]
    subprocess.run(command, cwd=ROOT, check=True)
    return out / "probe.bundle"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--guest-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.repetitions <= 0:
        parser.error("repetitions must be positive")
    manifest = {"rate": "100mbit", "rtt_ms": 20.0, "dualpi2": {
        "target": "15ms", "tupdate": "16ms", "step_thresh": "5ms"},
        "backlog_bdp": BACKLOG_GRID, "lead_rtt": LEAD_GRID, "repetitions": args.repetitions,
        "cells": []}
    for backlog in BACKLOG_GRID:
        for lead in LEAD_GRID:
            cell = args.output / f"b-{backlog:g}-d-{lead:g}"
            bundle = prepare(args.source_bundle, cell, backlog, lead)
            info = bundle_summary(bundle)
            probe = json.loads((cell / "probe-manifest.json").read_text())
            fraction = probe["important_bytes"] / (probe["important_bytes"] + probe["backlog_bytes"])
            command = [sys.executable, str(ROOT / "tools/l4s/run_qemu_3dgs_reordering.py"),
                       "--source-bundle", str(bundle), "--frozen-demand",
                       str(cell / "frozen-demand-order.json"), "--l4s-fractions",
                       f"{fraction:.12f},1",
                       "--repetitions", str(args.repetitions), "--deadline-ms", "3000",
                       "--qemu-memory", "2048", "--qemu-cpus", "2", "--guest-timeout",
                       str(args.guest_timeout), "--destination-prefix", f"backlog-b{backlog:g}-d{lead:g}",
                       "--allow-dirty", "--", "--downstream-mode", "dualpi2",
                       "--l4s-rate", "300mbit", "--classic-rate", "100mbit",
                       "--dc-background-mbps", "0", "--prague-warmup-ms", "0",
                       "--initial-visibility-spread-ms", "0", "--dualpi2-target", "15ms",
                       "--dualpi2-tupdate", "16ms", "--dualpi2-step", "5ms",
                       "--base-rtt-ms", "20", "--capture-mode", "packet-log"]
            manifest["cells"].append({"backlog_bdp": backlog, "lead_rtt": lead,
                                       "bundle": str(bundle), "bundle_summary": info,
                                       "command": command})
            if not args.dry_run:
                subprocess.run(command, cwd=ROOT, check=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "controlled-grid-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
