#!/usr/bin/env python3
"""Semantically split 3DGS Base and Enhancement traffic for reordering tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from split_3dgs_priority import _ranking, object_importance
from three_dgs_bundle import read_bundle, sha256_file, write_bundle


def _key(row: dict[str, object]) -> tuple[object, ...]:
    return (
        row["track_id"],
        row["group_id"],
        int(row["subgroup_id"]),
        int(row["object_id"]),
    )


def split_base_enhancement(
    source: Path,
    output_dir: Path,
    *,
    enhancement_l4s_fraction: float,
    importance: str = "native-tier",
) -> dict[str, object]:
    """Keep Base on Prague and sweep only Enhancement between Prague and Reno.

    subgroup 0 is treated as the progressive Base layer already defined by the
    pinned 3DGS preprocessing. subgroups 1 and 2 are Enhancement. Base always
    uses the dedicated Prague connection. ``enhancement_l4s_fraction`` selects
    the ranked prefix of Enhancement payload bytes that moves to a second
    Prague connection; the remainder stays on Reno/Classic.

    The two endpoint controls therefore both keep *two connections*:
      0.0 -> Base/Prague + Enhancement/Reno
      1.0 -> Base/Prague + Enhancement/Prague

    This avoids confounding queue-class treatment with connection count or an
    independent congestion-window advantage.
    """
    if not 0.0 <= enhancement_l4s_fraction <= 1.0:
        raise ValueError("enhancement_l4s_fraction must be in [0,1]")
    if importance not in ("native-tier", "opacity", "scale", "opacity-scale"):
        raise ValueError("unsupported importance mode")

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

    ranked, scores, definition = _ranking(rows, importance)
    rank_position = {source_index: rank for rank, source_index in enumerate(ranked)}

    base_indices = {
        index for index, row in enumerate(rows) if int(row["subgroup_id"]) == 0
    }
    enhancement_indices = set(range(len(rows))) - base_indices
    if not base_indices:
        raise ValueError("source has no subgroup-0 Base objects")
    if not enhancement_indices:
        raise ValueError("source has no Enhancement objects (subgroups 1/2)")

    ranked_enhancement = [index for index in ranked if index in enhancement_indices]
    enhancement_bytes = sum(int(rows[index]["payload_bytes"]) for index in enhancement_indices)
    prefix = [0]
    for index in ranked_enhancement:
        prefix.append(prefix[-1] + int(rows[index]["payload_bytes"]))
    target = enhancement_bytes * enhancement_l4s_fraction
    if enhancement_l4s_fraction == 0.0:
        promoted_count = 0
    elif enhancement_l4s_fraction == 1.0:
        promoted_count = len(ranked_enhancement)
    else:
        promoted_count = min(
            range(len(ranked_enhancement) + 1),
            key=lambda count: (abs(prefix[count] - target), count),
        )
    enhancement_prague = set(ranked_enhancement[:promoted_count])
    enhancement_reno = enhancement_indices - enhancement_prague

    base_path = output_dir / "base-prague.bundle"
    enh_prague_path = output_dir / "enh-prague.bundle"
    enh_reno_path = output_dir / "enh-reno.bundle"

    def selected(indices: set[int]) -> Iterable[bytes]:
        for index, payload in enumerate(payloads):
            if index in indices:
                yield payload

    base_disk = write_bundle(base_path, selected(base_indices))
    enh_prague_disk = write_bundle(enh_prague_path, selected(enhancement_prague))
    enh_reno_disk = write_bundle(enh_reno_path, selected(enhancement_reno))

    base_bytes = int(base_disk["payload_bytes"])
    enh_prague_bytes = int(enh_prague_disk["payload_bytes"])
    enh_reno_bytes = int(enh_reno_disk["payload_bytes"])
    actual_enh_fraction = enh_prague_bytes / enhancement_bytes
    total_l4s_bytes = base_bytes + enh_prague_bytes

    manifest_rows = []
    for index, row in enumerate(rows):
        subgroup = int(row["subgroup_id"])
        if index in base_indices:
            path = "high-prague"
            transport = "prague"
            semantic_layer = "base"
        elif index in enhancement_prague:
            path = "enh-prague"
            transport = "prague"
            semantic_layer = "enhancement"
        else:
            path = "low-reno"
            transport = "reno"
            semantic_layer = "enhancement"
        manifest_rows.append(
            {
                **row,
                "importance_score": scores[index],
                "importance_rank": rank_position[index],
                "semantic_layer": semantic_layer,
                "path": path,
                "transport": transport,
                "priority": "high" if subgroup == 0 else "low",
            }
        )

    manifest: dict[str, object] = {
        "schema_version": 3,
        "source_bundle": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "source_objects": len(rows),
        "source_payload_bytes": total_bytes,
        "source_gaussians": total_gaussians,
        "importance": importance,
        "importance_definition": definition,
        "semantic_rule": "subgroup 0 is Base; subgroups 1/2 are Enhancement",
        "transport_rule": (
            "Base always -> dedicated Prague/ECT(1); ranked Enhancement prefix -> "
            "second Prague/ECT(1); remaining Enhancement -> Reno/Not-ECT"
        ),
        "requested_enhancement_l4s_fraction": enhancement_l4s_fraction,
        "actual_enhancement_l4s_fraction": actual_enh_fraction,
        "actual_total_l4s_byte_fraction": total_l4s_bytes / total_bytes,
        "base_byte_fraction": base_bytes / total_bytes,
        "object_boundaries_preserved": True,
        "base": {
            **base_disk,
            "path": str(base_path),
            "sha256": sha256_file(base_path),
            "transport": "prague",
            "port": 4444,
        },
        "enhancement_l4s": {
            **enh_prague_disk,
            "path": str(enh_prague_path),
            "sha256": sha256_file(enh_prague_path),
            "transport": "prague",
            "port": 4445,
        },
        "enhancement_classic": {
            **enh_reno_disk,
            "path": str(enh_reno_path),
            "sha256": sha256_file(enh_reno_path),
            "transport": "reno",
            "port": 4443,
        },
        "balance": {
            "requested_enhancement_l4s_fraction": enhancement_l4s_fraction,
            "actual_enhancement_l4s_fraction": actual_enh_fraction,
            "actual_total_l4s_byte_fraction": total_l4s_bytes / total_bytes,
            "base_byte_fraction": base_bytes / total_bytes,
            "base_payload_bytes": base_bytes,
            "enhancement_payload_bytes": enhancement_bytes,
            "enhancement_l4s_payload_bytes": enh_prague_bytes,
            "enhancement_classic_payload_bytes": enh_reno_bytes,
        },
        "objects": manifest_rows,
    }
    (output_dir / "semantic-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
