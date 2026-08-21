#!/usr/bin/env python3
"""Two-connection 3DGS L4S spectrum split for post-send reordering tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from split_3dgs_priority import object_importance
from three_dgs_bundle import read_bundle, sha256_file, write_bundle


def _key(row: dict[str, object]) -> tuple[object, ...]:
    return (
        row["track_id"],
        row["group_id"],
        int(row["subgroup_id"]),
        int(row["object_id"]),
    )


def _layer_opacity_ranking(
    rows: list[dict[str, object]],
) -> tuple[list[int], list[float], str]:
    """Return best-first indices using (layer asc, mean opacity desc, source order).

    ``subgroup_id`` is the progressive layer exported by the pinned 3DGS
    preprocessing. Opacity is the mean encoded opacity logit and is used only as
    a monotone within-layer ordering signal.
    """
    if any(row["opacity_score"] is None for row in rows):
        raise ValueError(
            "layer-opacity ranking requires opacity arrays in every 3DGS object"
        )

    ranked = sorted(
        range(len(rows)),
        key=lambda index: (
            int(rows[index]["subgroup_id"]),
            -float(rows[index]["opacity_score"]),
            int(rows[index]["source_record_index"]),
        ),
    )
    rank_position = {
        source_index: rank for rank, source_index in enumerate(ranked)
    }
    scores = [
        float(len(rows) - rank_position[index]) / max(len(rows), 1)
        for index in range(len(rows))
    ]
    definition = (
        "progressive layer/subgroup ascending, then mean encoded opacity logit "
        "descending, then stable source order"
    )
    return ranked, scores, definition


def split_l4s_spectrum(
    source: Path,
    output_dir: Path,
    *,
    l4s_fraction: float,
) -> dict[str, object]:
    """Assign a ranked payload-byte prefix to Prague and the rest to Reno.

    Exactly two transport classes exist for every fraction. At 0 the Prague
    bundle is empty; at 1 the Reno bundle is empty. The experiment runner still
    establishes both QUIC connections in both endpoint controls.
    """
    if not 0.0 <= l4s_fraction <= 1.0:
        raise ValueError("l4s_fraction must be in [0,1]")

    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    payloads = list(read_bundle(source))
    if len(payloads) < 2:
        raise ValueError("reordering experiment requires at least two objects")

    rows: list[dict[str, object]] = []
    identities: set[tuple[object, ...]] = set()
    total_bytes = 0
    total_gaussians = 0
    for source_index, payload in enumerate(payloads):
        row = object_importance(payload)
        key = _key(row)
        if key in identities:
            raise ValueError(f"duplicate object identity: {key}")
        identities.add(key)
        row["source_record_index"] = source_index
        row["payload_bytes"] = len(payload)
        rows.append(row)
        total_bytes += len(payload)
        total_gaussians += int(row["num_gaussians"])

    ranked, scores, definition = _layer_opacity_ranking(rows)
    prefix_bytes = [0]
    for index in ranked:
        prefix_bytes.append(prefix_bytes[-1] + int(rows[index]["payload_bytes"]))

    target_bytes = total_bytes * l4s_fraction
    if l4s_fraction == 0.0:
        prague_count = 0
    elif l4s_fraction == 1.0:
        prague_count = len(rows)
    else:
        prague_count = min(
            range(1, len(rows)),
            key=lambda count: (
                abs(prefix_bytes[count] - target_bytes),
                count,
            ),
        )

    prague_indices = set(ranked[:prague_count])
    reno_indices = set(range(len(rows))) - prague_indices

    def selected(indices: set[int]) -> Iterable[bytes]:
        for index, payload in enumerate(payloads):
            if index in indices:
                yield payload

    prague_path = output_dir / "prague.bundle"
    reno_path = output_dir / "reno.bundle"
    prague_disk = write_bundle(prague_path, selected(prague_indices))
    reno_disk = write_bundle(reno_path, selected(reno_indices))

    rank_position = {
        source_index: rank for rank, source_index in enumerate(ranked)
    }
    manifest_rows: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        on_prague = index in prague_indices
        manifest_rows.append(
            {
                **row,
                "layer": int(row["subgroup_id"]),
                "mean_opacity": float(row["opacity_score"]),
                "importance_score": scores[index],
                "importance_rank": rank_position[index],
                "path": "high-prague" if on_prague else "low-reno",
                "transport": "prague" if on_prague else "reno",
            }
        )

    actual_fraction = int(prague_disk["payload_bytes"]) / total_bytes
    manifest: dict[str, object] = {
        "schema_version": 4,
        "source_bundle": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "source_objects": len(rows),
        "source_payload_bytes": total_bytes,
        "source_gaussians": total_gaussians,
        "priority_definition": definition,
        "split_rule": (
            "rank all objects by (layer, mean opacity, stable source order); "
            "ranked payload-byte prefix nearest requested fraction -> Prague; "
            "remainder -> Reno"
        ),
        "requested_l4s_byte_fraction": l4s_fraction,
        "actual_l4s_byte_fraction": actual_fraction,
        "fraction_error": actual_fraction - l4s_fraction,
        "object_boundaries_preserved": True,
        "prague": {
            **prague_disk,
            "path": str(prague_path),
            "sha256": sha256_file(prague_path),
            "transport": "prague",
            "port": 4444,
        },
        "reno": {
            **reno_disk,
            "path": str(reno_path),
            "sha256": sha256_file(reno_path),
            "transport": "reno",
            "port": 4443,
        },
        "balance": {
            "requested_l4s_byte_fraction": l4s_fraction,
            "actual_l4s_byte_fraction": actual_fraction,
            "prague_payload_bytes": int(prague_disk["payload_bytes"]),
            "reno_payload_bytes": int(reno_disk["payload_bytes"]),
            "prague_objects": int(prague_disk["objects"]),
            "reno_objects": int(reno_disk["objects"]),
        },
        "objects": manifest_rows,
    }
    (output_dir / "spectrum-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
