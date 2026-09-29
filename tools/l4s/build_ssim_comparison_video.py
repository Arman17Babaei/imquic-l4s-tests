#!/usr/bin/env python3
"""Build a frame-synchronized Reference/L4S/Classic comparison video."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path


FRAME_COUNT = 465
SOURCE_FRAME_RATE = Fraction(232500, 5147)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_sequence(path: Path) -> None:
    missing = [index for index in range(FRAME_COUNT) if not (path / f"frame_{index:04d}.png").is_file()]
    if missing:
        raise SystemExit(f"{path}: missing frame indices {missing[:10]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-frames", type=Path, required=True)
    parser.add_argument("--l4s-frames", type=Path, required=True)
    parser.add_argument("--classic-frames", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--slowdown", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.slowdown < 1:
        raise SystemExit("--slowdown must be at least 1")
    output_frame_rate = SOURCE_FRAME_RATE / args.slowdown
    frame_rate = f"{output_frame_rate.numerator}/{output_frame_rate.denominator}"

    for frames in (args.reference_frames, args.l4s_frames, args.classic_frames):
        validate_sequence(frames.resolve())
    if not args.font.is_file():
        raise SystemExit(f"font not found: {args.font}")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    font = args.font.resolve().as_posix().replace("'", "\\'")
    filter_graph = ";".join((
        f"[0:v]scale=640:360:flags=lanczos,pad=640:414:0:54:white,drawtext=fontfile='{font}':text='Reference':fontcolor=black:fontsize=32:x=(w-text_w)/2:y=10[reference]",
        f"[1:v]scale=640:360:flags=lanczos,pad=640:414:0:54:white,drawtext=fontfile='{font}':text='L4S':fontcolor=black:fontsize=32:x=(w-text_w)/2:y=10[l4s]",
        f"[2:v]scale=640:360:flags=lanczos,pad=640:414:0:54:white,drawtext=fontfile='{font}':text='Classic':fontcolor=black:fontsize=32:x=(w-text_w)/2:y=10[classic]",
        "[reference][l4s][classic]hstack=inputs=3,format=yuv420p[video]",
    ))
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    for frames in (args.reference_frames, args.l4s_frames, args.classic_frames):
        command.extend(("-framerate", frame_rate, "-start_number", "0", "-i", str(frames / "frame_%04d.png")))
    command.extend((
        "-filter_complex", filter_graph,
        "-map", "[video]",
        "-frames:v", str(FRAME_COUNT),
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-movflags", "+faststart",
        str(args.output),
    ))
    subprocess.run(command, check=True)

    metadata = {
        "output": str(args.output),
        "panel_order": ["Reference", "L4S", "Classic"],
        "synchronization": "Frame index; all panels use frame_NNNN at the same output frame.",
        "frame_count": FRAME_COUNT,
        "source_frame_rate": f"{SOURCE_FRAME_RATE.numerator}/{SOURCE_FRAME_RATE.denominator}",
        "playback_slowdown": args.slowdown,
        "frame_rate": frame_rate,
        "duration_seconds": FRAME_COUNT / float(output_frame_rate),
        "sources": {
            "reference": str(args.reference_frames.resolve()),
            "l4s": str(args.l4s_frames.resolve()),
            "classic": str(args.classic_frames.resolve()),
        },
        "sha256": sha256(args.output),
    }
    args.output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
