#!/usr/bin/env python3
"""Convert a standard Graphdeco 3DGS PLY to Spark's 32-byte SPLAT format."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

SH_C0 = 0.28209479177387814
EXPECTED_PROPERTIES = ["x", "y", "z", "nx", "ny", "nz"] + [f"f_dc_{i}" for i in range(3)] + [
    f"f_rest_{i}" for i in range(45)
] + ["opacity"] + [f"scale_{i}" for i in range(3)] + [f"rot_{i}" for i in range(4)]


def parse_header(source: Path) -> tuple[int, int]:
    with source.open("rb") as stream:
        header = bytearray()
        while not header.endswith(b"end_header\n"):
            block = stream.readline()
            if not block or len(header) > 1024 * 1024:
                raise ValueError("missing or oversized PLY header")
            header.extend(block)
    lines = header.decode("ascii").splitlines()
    if "format binary_little_endian 1.0" not in lines:
        raise ValueError("only binary little-endian PLY is supported")
    vertices = next((int(line.split()[2]) for line in lines if line.startswith("element vertex ")), None)
    properties = [line.split()[2] for line in lines if line.startswith("property float ")]
    if vertices is None or properties != EXPECTED_PROPERTIES:
        raise ValueError("PLY does not have the expected Graphdeco degree-3 vertex layout")
    return len(header), vertices


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def convert(source: Path, output: Path, chunk_size: int, part_splats: int) -> dict[str, object]:
    header_bytes, count = parse_header(source)
    record_bytes = len(EXPECTED_PROPERTIES) * 4
    if source.stat().st_size != header_bytes + count * record_bytes:
        raise ValueError("PLY size does not match its vertex declaration")
    vertices = np.memmap(source, dtype="<f4", mode="r", offset=header_bytes, shape=(count, len(EXPECTED_PROPERTIES)))
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    minimum = np.full(3, np.inf)
    maximum = np.full(3, -np.inf)
    parts: list[dict[str, str | int]] = []
    target = None
    part_count = 0
    part_path = output
    try:
        start = 0
        while start < count:
            if target is None or (part_splats > 0 and part_count >= part_splats):
                if target is not None:
                    target.close()
                part_path = output if part_splats == 0 else output.with_name(f"{output.stem}-{len(parts):03d}{output.suffix}")
                target = part_path.open("wb")
                part_count = 0
                parts.append({"path": part_path.name, "gaussian_count": 0})
            take = min(chunk_size, count - start, part_splats - part_count if part_splats > 0 else chunk_size)
            values = np.asarray(vertices[start:start + take])
            positions = values[:, 0:3]
            scales = np.exp(values[:, 55:58]).astype("<f4")
            colors = np.clip((values[:, 6:9] * SH_C0 + 0.5) * 255, 0, 255).astype(np.uint8)
            alpha = np.clip(255 / (1 + np.exp(-values[:, 54])), 0, 255).astype(np.uint8)
            rotations = values[:, 58:62]
            rotations = rotations / np.maximum(np.linalg.norm(rotations, axis=1, keepdims=True), 1e-12)
            quaternion = np.clip(rotations * 128 + 128, 0, 255).astype(np.uint8)
            records = np.empty((len(values), 32), dtype=np.uint8)
            records[:, 0:12] = positions.astype("<f4").view(np.uint8).reshape(-1, 12)
            records[:, 12:24] = scales.view(np.uint8).reshape(-1, 12)
            records[:, 24:27] = colors
            records[:, 27] = alpha
            records[:, 28:32] = quaternion
            encoded = records.tobytes()
            target.write(encoded)
            part_count += len(values)
            parts[-1]["gaussian_count"] = part_count
            digest.update(encoded)
            minimum = np.minimum(minimum, positions.min(axis=0))
            maximum = np.maximum(maximum, positions.max(axis=0))
            start += len(values)
    finally:
        if target is not None:
            target.close()
    total_bytes = sum((output.parent / str(part["path"])).stat().st_size for part in parts)
    return {"gaussian_count": count, "bytes": total_bytes, "sha256": digest.hexdigest(), "parts": parts,
            "min": minimum.tolist(), "max": maximum.tolist(), "representation": "antimatter15-splat",
            "preserved": ["position", "scale", "rotation", "opacity", "dc_color"], "omitted": ["sh1", "sh2", "sh3"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chunk-size", type=int, default=65536)
    parser.add_argument("--part-splats", type=int, default=0)
    args = parser.parse_args()
    if args.chunk_size < 1:
        parser.error("--chunk-size must be positive")
    if args.part_splats < 0:
        parser.error("--part-splats must be non-negative")
    metadata = convert(args.input.resolve(), args.output.resolve(), args.chunk_size, args.part_splats)
    provenance = {"source": str(args.input.resolve()), "source_sha256": sha256_file(args.input),
                  "output": str(args.output.resolve()), **metadata}
    args.output.with_suffix(".json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
