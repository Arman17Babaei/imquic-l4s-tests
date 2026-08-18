#!/usr/bin/env python3
"""Split a prepared 3DGS bundle into high/low object-priority halves."""

from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path
from typing import Iterator

from three_dgs_bundle import read_bundle, sha256_file, write_bundle

FRAME_HEADER = struct.Struct("<8I")
FRAME_MAGIC = 0x47535033
FRAME_VERSION = 1
ARRAY_LENGTH = struct.Struct("<I")
FLOAT_SIZE = 4
ARRAY_NAMES = ("means", "opacities", "sh_coeffs", "scales", "rotations")


def object_identity(payload: bytes) -> dict[str, object]:
    """Parse the stable embedded identity without decoding Gaussian tensors."""
    if len(payload) < FRAME_HEADER.size:
        raise ValueError("3DGS object shorter than frame header")
    magic, version, track_len, group_len, object_id, num_gaussians, payload_len, subgroup_id = (
        FRAME_HEADER.unpack_from(payload, 0)
    )
    if magic != FRAME_MAGIC or version != FRAME_VERSION:
        raise ValueError("unsupported 3DGS object")
    declared_end = FRAME_HEADER.size + payload_len
    group_end = FRAME_HEADER.size + track_len + group_len
    if declared_end != len(payload) or group_end > declared_end:
        raise ValueError("3DGS object payload length mismatch")
    track_end = FRAME_HEADER.size + track_len
    try:
        track_id = payload[FRAME_HEADER.size:track_end].decode("utf-8")
        group_id = payload[track_end:group_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("3DGS object has invalid track/group identity") from error
    if subgroup_id > 2:
        raise ValueError(f"unsupported 3DGS subgroup_id {subgroup_id}")
    return {
        "track_id": track_id,
        "group_id": group_id,
        "object_id": int(object_id),
        "subgroup_id": int(subgroup_id),
        "num_gaussians": int(num_gaussians),
        "payload_len": int(payload_len),
        "track_len": int(track_len),
        "group_len": int(group_len),
    }


def _array_views(payload: bytes, identity: dict[str, object]) -> dict[str, memoryview | None]:
    """Parse the real 3dgs_over_moq length-prefixed attribute layout.

    protocol.py encodes five arrays in fixed order; each starts with a uint32
    byte length and a zero length means that attribute is intentionally absent.
    """
    offset = FRAME_HEADER.size + int(identity["track_len"]) + int(identity["group_len"])
    end = FRAME_HEADER.size + int(identity["payload_len"])
    result: dict[str, memoryview | None] = {}
    view = memoryview(payload)
    for name in ARRAY_NAMES:
        if offset + ARRAY_LENGTH.size > end:
            raise ValueError(f"3DGS object missing {name} length prefix")
        (length,) = ARRAY_LENGTH.unpack_from(payload, offset)
        offset += ARRAY_LENGTH.size
        if offset + length > end:
            raise ValueError(f"3DGS object has truncated {name} array")
        if length and length % FLOAT_SIZE:
            raise ValueError(f"3DGS object {name} length is not float32-aligned")
        result[name] = view[offset:offset + length] if length else None
        offset += length
    if offset != end:
        raise ValueError("3DGS object has trailing bytes after attribute arrays")
    return result


def _float_values(array: memoryview | None, expected: int, name: str) -> Iterator[float]:
    if array is None:
        raise ValueError(f"3DGS object does not carry {name}; cannot rank by it")
    if len(array) != expected * FLOAT_SIZE:
        raise ValueError(
            f"3DGS object {name} has {len(array) // FLOAT_SIZE} floats; expected {expected}"
        )
    for (value,) in struct.iter_unpack("<f", array):
        if not math.isfinite(value):
            raise ValueError(f"3DGS object has non-finite {name}")
        yield float(value)


def object_importance(payload: bytes) -> dict[str, object]:
    """Return dependency-light metadata plus opacity/scale ablation scores.

    Opacity is stored in the source cache as a pre-sigmoid logit, so its mean is
    used only as an ordering statistic. Scale is stored in log-space; the mean
    maximum log-scale axis is likewise an ordering statistic. Both transforms
    are monotone and preserve within-attribute rank without changing the wire data.
    """
    identity = object_identity(payload)
    num_gaussians = int(identity["num_gaussians"])
    if num_gaussians <= 0:
        raise ValueError("3DGS object contains no Gaussians")
    arrays = _array_views(payload, identity)

    opacity_score = None
    if arrays["opacities"] is not None:
        opacity_values = list(_float_values(arrays["opacities"], num_gaussians, "opacities"))
        opacity_score = sum(opacity_values) / num_gaussians

    scale_score = None
    if arrays["scales"] is not None:
        scale_values = list(_float_values(arrays["scales"], num_gaussians * 3, "scales"))
        scale_maxima = [max(scale_values[i:i + 3]) for i in range(0, len(scale_values), 3)]
        scale_score = sum(scale_maxima) / num_gaussians

    return {
        "track_id": identity["track_id"],
        "group_id": identity["group_id"],
        "object_id": identity["object_id"],
        "subgroup_id": identity["subgroup_id"],
        "num_gaussians": num_gaussians,
        "opacity_score": opacity_score,
        "scale_score": scale_score,
    }


def _rank_scores(values: list[float]) -> list[float]:
    """Map values to stable [0,1] ranks, keeping source order as tie-breaker."""
    if not values:
        return []
    if len(values) == 1:
        return [1.0]
    order = sorted(range(len(values)), key=lambda index: (values[index], -index))
    result = [0.0] * len(values)
    for rank, index in enumerate(order):
        result[index] = rank / (len(values) - 1)
    return result


def _ranking(rows: list[dict[str, object]], importance: str) -> tuple[list[int], list[float], str]:
    """Return best-first source indices, display scores, and a human-readable rule."""
    if importance == "native-tier":
        # The pinned MoQSplat preprocessing defines subgroup 0 -> 1 -> 2 as the
        # progressive importance order. Mean opacity is only a deterministic
        # tie-break inside the already-computed tier, not a replacement for it.
        ranked = sorted(
            range(len(rows)),
            key=lambda i: (
                int(rows[i]["subgroup_id"]),
                0 if rows[i]["opacity_score"] is not None else 1,
                -float(rows[i]["opacity_score"]) if rows[i]["opacity_score"] is not None else 0.0,
                i,
            ),
        )
        # Ordinal display score: tier dominates; opacity only resolves ties when present.
        present_opacities = [
            float(row["opacity_score"]) if row["opacity_score"] is not None else float("-inf")
            for row in rows
        ]
        finite = [value for value in present_opacities if math.isfinite(value)]
        if finite:
            floor = min(finite) - 1.0
            opacity_ranks = _rank_scores([value if math.isfinite(value) else floor for value in present_opacities])
        else:
            opacity_ranks = [0.0] * len(rows)
        scores = [float(2 - int(row["subgroup_id"])) + opacity_ranks[i] / 2.0 for i, row in enumerate(rows)]
        definition = "native subgroup tier (0 > 1 > 2), then mean opacity logit within tier"
        return ranked, scores, definition

    if importance == "opacity-scale":
        if any(row["opacity_score"] is None or row["scale_score"] is None for row in rows):
            raise ValueError("opacity-scale ranking requires both opacity and scale arrays in every object")
        opacity_ranks = _rank_scores([float(row["opacity_score"]) for row in rows])
        scale_ranks = _rank_scores([float(row["scale_score"]) for row in rows])
        scores = [0.5 * (opacity_ranks[i] + scale_ranks[i]) for i in range(len(rows))]
        ranked = sorted(range(len(rows)), key=lambda i: (-scores[i], i))
        return ranked, scores, "equal-weight percentile-rank fusion of opacity and scale"

    if any(row[f"{importance}_score"] is None for row in rows):
        raise ValueError(f"{importance} ranking requires that array in every object")
    scores = [float(row[f"{importance}_score"]) for row in rows]
    ranked = sorted(range(len(rows)), key=lambda i: (-scores[i], i))
    definition = (
        "mean encoded opacity logit per object"
        if importance == "opacity"
        else "mean maximum encoded log-scale axis per object"
    )
    return ranked, scores, definition


def _aggregate(rows: list[dict[str, object]], selected: set[int]) -> dict[str, object]:
    subgroup_objects = {str(i): 0 for i in range(3)}
    subgroup_gaussians = {str(i): 0 for i in range(3)}
    payload_bytes = 0
    gaussians = 0
    for index, row in enumerate(rows):
        if index not in selected:
            continue
        subgroup = str(int(row["subgroup_id"]))
        count = int(row["num_gaussians"])
        subgroup_objects[subgroup] += 1
        subgroup_gaussians[subgroup] += count
        payload_bytes += int(row["payload_bytes"])
        gaussians += count
    return {
        "objects": len(selected),
        "payload_bytes": payload_bytes,
        "gaussians": gaussians,
        "subgroup_objects": subgroup_objects,
        "subgroup_gaussians": subgroup_gaussians,
    }


def split_priority_bundle(
    source: Path,
    output_dir: Path,
    *,
    importance: str = "native-tier",
) -> dict[str, object]:
    """Rank whole MoQ objects and split exactly by object count.

    Object boundaries are preserved. The best floor(N/2) objects go to Prague;
    the remainder go to Reno. Inside each output bundle, original source order
    is retained so the experiment changes path assignment, not application order.
    """
    if importance not in ("native-tier", "opacity", "scale", "opacity-scale"):
        raise ValueError("importance must be native-tier, opacity, scale, or opacity-scale")
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    identities: set[tuple[object, ...]] = set()
    for source_index, payload in enumerate(read_bundle(source)):
        row = object_importance(payload)
        identity = (
            row["track_id"], row["group_id"], row["subgroup_id"], row["object_id"],
        )
        if identity in identities:
            raise ValueError(f"duplicate 3DGS object identity in source bundle: {identity}")
        identities.add(identity)
        row["source_record_index"] = source_index
        row["payload_bytes"] = len(payload)
        rows.append(row)
    if len(rows) < 2:
        raise ValueError("priority split requires at least two source objects")

    ranked, scores, definition = _ranking(rows, importance)
    high_count = len(rows) // 2
    high_indices = set(ranked[:high_count])
    low_indices = set(range(len(rows))) - high_indices

    high_path = output_dir / "high-priority.bundle"
    low_path = output_dir / "low-priority.bundle"

    def selected(indices: set[int]):
        for index, payload in enumerate(read_bundle(source)):
            if index in indices:
                yield payload

    high_disk = write_bundle(high_path, selected(high_indices))
    low_disk = write_bundle(low_path, selected(low_indices))
    high_stats = _aggregate(rows, high_indices)
    low_stats = _aggregate(rows, low_indices)
    if high_disk["objects"] != high_stats["objects"] or low_disk["objects"] != low_stats["objects"]:
        raise RuntimeError("split bundle object totals disagree with manifest")
    if high_disk["payload_bytes"] != high_stats["payload_bytes"] or low_disk["payload_bytes"] != low_stats["payload_bytes"]:
        raise RuntimeError("split bundle byte totals disagree with manifest")

    high_position = 0
    low_position = 0
    rank_position = {source_index: rank for rank, source_index in enumerate(ranked)}
    manifest_rows = []
    for index, row in enumerate(rows):
        high = index in high_indices
        path_record_index = high_position if high else low_position
        if high:
            high_position += 1
        else:
            low_position += 1
        manifest_rows.append({
            **row,
            "importance_score": scores[index],
            "importance_rank": rank_position[index],
            "priority": "high" if high else "low",
            "transport": "prague" if high else "reno",
            "path_record_index": path_record_index,
        })

    source_totals = _aggregate(rows, set(range(len(rows))))
    manifest: dict[str, object] = {
        "schema_version": 2,
        "source_bundle": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "source_objects": len(rows),
        "source_payload_bytes": source_totals["payload_bytes"],
        "source_gaussians": source_totals["gaussians"],
        "importance": importance,
        "importance_definition": definition,
        "split_rule": "best floor(N/2) objects -> high/Prague; remainder -> low/Reno",
        "object_boundaries_preserved": True,
        "source_order_preserved_within_each_output": True,
        "high": {
            **high_disk,
            **{key: value for key, value in high_stats.items() if key not in ("objects", "payload_bytes")},
            "sha256": sha256_file(high_path),
            "path": str(high_path),
            "transport": "prague",
        },
        "low": {
            **low_disk,
            **{key: value for key, value in low_stats.items() if key not in ("objects", "payload_bytes")},
            "sha256": sha256_file(low_path),
            "path": str(low_path),
            "transport": "reno",
        },
        "balance": {
            "object_fraction_high": high_stats["objects"] / len(rows),
            "byte_fraction_high": high_stats["payload_bytes"] / int(source_totals["payload_bytes"]),
            "gaussian_fraction_high": high_stats["gaussians"] / int(source_totals["gaussians"]),
        },
        "objects": manifest_rows,
    }
    manifest_path = output_dir / "priority-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--importance",
        choices=("native-tier", "opacity", "scale", "opacity-scale"),
        default="native-tier",
    )
    args = parser.parse_args()
    result = split_priority_bundle(args.source_bundle, args.output_dir, importance=args.importance)
    print(json.dumps({key: value for key, value in result.items() if key != "objects"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
