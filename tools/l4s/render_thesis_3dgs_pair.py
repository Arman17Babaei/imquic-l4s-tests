#!/usr/bin/env python3
"""Render a matched Classic/L4S pair against one shared full-scene reference."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import render_3dgs_reordering as render


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument(
        "--reference-root",
        type=Path,
        help="reuse a previously completed 465-frame full-scene reference",
    )
    parser.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--ssim-device", default="cuda:0")
    parser.add_argument("--max-gaussians-per-pass", type=int, default=400000)
    parser.add_argument("--gaussian-budget", type=int, default=5000000)
    args = parser.parse_args()
    render.configure_render_logging()
    render.require_dependency(args.three_dgs_dir, allow_unpinned=True)
    render.activate_render_dependency(args.three_dgs_dir)
    reference_root = args.reference_root or args.root / "reference-trace"
    reference_pngs = sorted((reference_root / "frames/png").glob("frame_*.png"))
    if len(reference_pngs) == 465:
        from streaming.transport.client.viewport.trace import load_trace
        frames = load_trace(args.trace)
        references = {int(path.stem.split("_")[-1]): path for path in reference_pngs}
    else:
        references, frames = render.render_references(
            args.source_bundle, args.cache, args.trace, reference_root,
            device=args.device, width=1920, height=1080, frame_step=1,
            frame_count=None, gaussian_budget=args.gaussian_budget,
            max_gaussians_per_pass=args.max_gaussians_per_pass,
        )
    results = {}
    for label in ("classic", "l4s"):
        cell = args.root / label
        cases = [path for path in cell.iterdir()
                 if path.is_dir() and (path / "result.json").is_file()]
        if len(cases) != 1:
            raise RuntimeError(f"{cell}: expected one completed case")
        frozen = json.loads((cell / "inputs/frozen-demand-order.json").read_text())
        quality_csv = cases[0] / "render-lag-0ms/frame-quality.csv"
        if quality_csv.is_file() and sum(1 for _ in quality_csv.open()) == 466:
            with quality_csv.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            ssims = [float(row["ssim"]) for row in rows]
            results[label] = {
                "case": cases[0].name, "frames": len(rows),
                "mean_ssim": sum(ssims) / len(ssims),
                "frame_quality_csv": str(quality_csv), "reused_complete_render": True,
            }
            continue
        results[label] = render.render_case(
            cases[0], cache_path=args.cache, reference_paths=references,
            frames=frames, frozen_demand=frozen, device=args.device,
            ssim_device=args.ssim_device, width=1920, height=1080,
            frame_step=1, frame_count=None, gaussian_budget=args.gaussian_budget,
            max_gaussians_per_pass=args.max_gaussians_per_pass,
            evaluation_lag_ms=0.0, gif_duration_ms=None, generate_gif=False,
        )
    (args.root / "quality-pair.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
