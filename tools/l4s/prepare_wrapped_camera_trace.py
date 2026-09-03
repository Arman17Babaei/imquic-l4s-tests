#!/usr/bin/env python3
"""Materialize the shifted/wrapped camera trace used by thesis Figure 10."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path, output: Path, *, start_ms: float, wrap_end_ms: float,
          fov_scale: float) -> dict:
    frames = json.loads(source.read_text())
    first = [dict(frame) for frame in frames if float(frame["timestamp_ms"]) >= start_ms]
    second = [dict(frame) for frame in frames if float(frame["timestamp_ms"]) <= wrap_end_ms]
    if not first or not second:
        raise ValueError("wrapped trace segments must both be non-empty")
    wrapped = first + second
    first_origin = float(first[0]["timestamp_ms"])
    first_duration = float(first[-1]["timestamp_ms"]) - first_origin
    for frame in first:
        frame["timestamp_ms"] = float(frame["timestamp_ms"]) - first_origin
        frame["fov"] = float(frame["fov"]) * fov_scale
    for frame in second:
        frame["timestamp_ms"] = first_duration + float(frame["timestamp_ms"])
        frame["fov"] = float(frame["fov"]) * fov_scale
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(wrapped, indent=2) + "\n")
    metadata = {
        "source": str(source.resolve()), "source_sha256": sha256(source),
        "output": str(output.resolve()), "output_sha256": sha256(output),
        "start_ms": start_ms, "wrap_end_ms": wrap_end_ms,
        "fov_scale": fov_scale, "frames": len(wrapped),
        "timestamp_policy": "preserve source intervals; concatenate at wrap",
        "wrap_frame_index": len(first), "wrap_time_ms": first_duration,
        "duration_ms": float(wrapped[-1]["timestamp_ms"]),
    }
    output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start-ms", type=float, default=7800.0)
    parser.add_argument("--wrap-end-ms", type=float, default=7000.0)
    parser.add_argument("--fov-scale", type=float, default=0.5)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, start_ms=args.start_ms,
                           wrap_end_ms=args.wrap_end_ms,
                           fov_scale=args.fov_scale), indent=2))


if __name__ == "__main__":
    main()
