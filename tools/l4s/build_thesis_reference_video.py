#!/usr/bin/env python3
"""Encode the full-scene reference frames using exact camera-trace timing."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trace = json.loads(args.trace.read_text())
    frames = sorted(args.frames.glob("frame_*.png"))
    if len(trace) != 465 or len(frames) != len(trace):
        raise ValueError("reference video requires 465 trace frames and PNGs")
    duration_seconds = float(trace[-1]["timestamp_ms"]) / 1000.0
    mean_rate = len(frames) / duration_seconds
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-framerate", f"{mean_rate:.12f}", "-start_number", "0",
        "-i", str(args.frames / "frame_%04d.png"),
        "-frames:v", str(len(frames)), "-c:v", "libx264", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.output),
    ], check=True)
    metadata = {
        "reference_mp4": str(args.output),
        "frames": len(frames),
        "trace_duration_ms": float(trace[-1]["timestamp_ms"]),
        "timing": (
            "465 frames at the trace-equivalent mean rate; exact irregular "
            "frame timestamps are preserved in the checksummed trace"
        ),
        "encoded_frame_rate": mean_rate,
        "sha256": {
            "reference_mp4": sha256(args.output),
            "trace": sha256(args.trace),
            "scene_bundle": sha256(args.scene),
        },
    }
    args.output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
