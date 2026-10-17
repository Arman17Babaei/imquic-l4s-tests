#!/usr/bin/env python3
"""Generate the fixed-position 50-second yaw trace from a source camera pose."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _player_coordinates(vector: list[float]) -> list[float]:
    """Keep source world coordinates, including view-dependent SH directions."""
    return [float(value) for value in vector]


def _yaw_at(timestamp_ms: int) -> float:
    if timestamp_ms <= 15_000:
        return -180.0 * timestamp_ms / 15_000.0
    if timestamp_ms <= 25_000:
        return -180.0 + 90.0 * (timestamp_ms - 15_000) / 10_000.0
    return -90.0 - 270.0 * (timestamp_ms - 25_000) / 25_000.0


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm < 1e-12:
        raise ValueError("camera basis contains a zero vector")
    return [value / norm for value in vector]


def _rotate_axis(vector: list[float], axis: list[float], degrees: float) -> list[float]:
    """Rotate a direction around the recorded camera-up axis."""
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    x, y, z = vector
    ux, uy, uz = axis
    dot = ux * x + uy * y + uz * z
    cross = [uy * z - uz * y, uz * x - ux * z, ux * y - uy * x]
    return [
        x * cosine + cross[0] * sine + ux * dot * (1 - cosine),
        y * cosine + cross[1] * sine + uy * dot * (1 - cosine),
        z * cosine + cross[2] * sine + uz * dot * (1 - cosine),
    ]


def generate_trace(source_frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not source_frames:
        raise ValueError("source trace is empty")
    first = source_frames[0]
    view = first["view_matrix"]
    initial_forward = _normalize(_player_coordinates([-float(view[2][axis]) for axis in range(3)]))
    recorded_up = _normalize(_player_coordinates([float(view[1][axis]) for axis in range(3)]))
    projection = sum(a * b for a, b in zip(recorded_up, initial_forward))
    yaw_axis = _normalize([value - projection * forward
                           for value, forward in zip(recorded_up, initial_forward)])
    yaw_axis = [-value for value in yaw_axis]
    camera_up = list(yaw_axis)
    position = _player_coordinates(first["camera_position"])
    frames = []
    for timestamp_ms in range(0, 50_001, 100):
        yaw = _yaw_at(timestamp_ms)
        frames.append({
            "timestamp_ms": timestamp_ms,
            "camera_position": position,
            "camera_forward": _rotate_axis(initial_forward, yaw_axis, yaw),
            "camera_up": camera_up,
            "yaw_degrees": yaw,
            "fov": float(first["fov"]),
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
