#!/usr/bin/env python3
"""Generate the fixed-position 50-second bicycle priority trace."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _player_coordinates(vector: list[float]) -> list[float]:
    """Apply the player's -90 degree X rotation."""
    return [float(vector[0]), float(vector[2]), -float(vector[1])]


def _yaw_at(timestamp_ms: int) -> float:
    if timestamp_ms <= 15_000:
        return -180.0 * timestamp_ms / 15_000.0
    if timestamp_ms <= 25_000:
        return -180.0 + 90.0 * (timestamp_ms - 15_000) / 10_000.0
    return -90.0 - 270.0 * (timestamp_ms - 25_000) / 25_000.0


def _rotate_y(vector: list[float], degrees: float) -> list[float]:
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    x, _, z = vector
    return [cosine * x + sine * z, 0.0, -sine * x + cosine * z]


def generate_trace(source_frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not source_frames:
        raise ValueError("source trace is empty")
    first = source_frames[0]
    view = first["view_matrix"]
    source_forward = [-float(view[2][axis]) for axis in range(3)]
    transformed = _player_coordinates(source_forward)
    horizontal = [transformed[0], 0.0, transformed[2]]
    norm = math.hypot(horizontal[0], horizontal[2])
    if norm < 1e-12:
        raise ValueError("first camera frame has no horizontal heading")
    initial_forward = [horizontal[0] / norm, 0.0, horizontal[2] / norm]
    position = _player_coordinates(first["camera_position"])
    frames = []
    for timestamp_ms in range(0, 50_001, 100):
        yaw = _yaw_at(timestamp_ms)
        frames.append({
            "timestamp_ms": timestamp_ms,
            "camera_position": position,
            "camera_forward": _rotate_y(initial_forward, yaw),
            "yaw_degrees": yaw,
            "fov": 40.0,
            "aspect_ratio": 16.0 / 9.0,
        })
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8"))
    frames = generate_trace(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(frames, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
