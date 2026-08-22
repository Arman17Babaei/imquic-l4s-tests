#!/usr/bin/env python3
"""Freeze 3DGS bicycle-trace demand before a network experiment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from reordering_workload import derive_first_visible_object_order

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_3DGS = Path(os.environ.get("THREEDGS_DIR", ROOT / "deps" / "3dgs_over_moq"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, default=DEFAULT_3DGS)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--allow-unpinned-3dgs", action="store_true")
    args = parser.parse_args()

    for path in (args.source_bundle, args.trace, args.three_dgs_dir):
        if not path.exists():
            raise SystemExit(f"missing required path: {path}")
    if args.width <= 0 or args.height <= 0 or args.frame_stride <= 0:
        parser.error("width, height, and frame stride must be positive")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")

    frozen = derive_first_visible_object_order(
        args.source_bundle,
        args.trace,
        args.three_dgs_dir,
        width=args.width,
        height=args.height,
        frame_stride=args.frame_stride,
        allow_unpinned=args.allow_unpinned_3dgs,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(frozen, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
