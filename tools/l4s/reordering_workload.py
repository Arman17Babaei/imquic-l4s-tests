#!/usr/bin/env python3
"""Deterministic 3DGS split and frozen camera-demand helpers."""

from __future__ import annotations

import csv
import json
import math
import runpy
from pathlib import Path
from typing import Iterable, Mapping

from split_3dgs_priority import (
    _array_views,
    _float_values,
    _ranking,
    object_identity,
    object_importance,
)
from three_dgs_bundle import (
    _activate_dependency,
    read_bundle,
    require_dependency,
    sha256_file,
    write_bundle,
)


def _object_key(row: dict[str, object]) -> tuple[object, ...]:
    return (
        row["track_id"],
        row["group_id"],
        int(row["subgroup_id"]),
        int(row["object_id"]),
    )


def _object_key_from_payload(payload: bytes) -> tuple[object, ...]:
    return _object_key(object_importance(payload))


def manifest_importance_ranks(
    manifest: Mapping[str, object],
) -> dict[tuple[object, ...], int]:
    """Return the manifest's unique global importance rank for every object."""
    ranks: dict[tuple[object, ...], int] = {}
    used: set[int] = set()
    for raw in manifest["objects"]:  # type: ignore[index]
        row = dict(raw)  # type: ignore[arg-type]
        key = _object_key(row)
        rank = int(row["importance_rank"])
        if key in ranks:
            raise ValueError(f"duplicate object identity in priority manifest: {key}")
        if rank in used:
            raise ValueError(f"duplicate importance rank in priority manifest: {rank}")
        ranks[key] = rank
        used.add(rank)
    return ranks


def _rank_for_payload(
    payload: bytes,
    importance_ranks: Mapping[tuple[object, ...], int],
) -> int:
    row = object_importance(payload)
    key = _object_key(row)
    if key not in importance_ranks:
        raise ValueError(f"bundle object missing from priority manifest: {key}")
    return int(importance_ranks[key])


def split_by_l4s_fraction(
    source: Path,
    output_dir: Path,
    *,
    l4s_fraction: float,
    importance: str = "native-tier",
) -> dict[str, object]:
    """Split a scene at a ranked object boundary closest to a target byte fraction.

    The top-ranked prefix is assigned to Prague/L4S and the remainder to
    Reno/Classic. Object payloads are not re-chunked. Within each output bundle,
    source order is preserved so path assignment is the only ordering change.
    """
    if not 0.0 <= l4s_fraction <= 1.0:
        raise ValueError("l4s_fraction must be in [0, 1]")
    if importance not in ("native-tier", "opacity", "scale", "opacity-scale"):
        raise ValueError("unsupported importance mode")

    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    payloads = list(read_bundle(source))
    if len(payloads) < 2:
        raise ValueError("reordering experiment requires at least two source objects")

    rows: list[dict[str, object]] = []
    identities: set[tuple[object, ...]] = set()
    total_bytes = 0
    total_gaussians = 0
    for source_index, payload in enumerate(payloads):
        row = object_importance(payload)
        key = _object_key(row)
        if key in identities:
            raise ValueError(f"duplicate 3DGS object identity in source bundle: {key}")
        identities.add(key)
        row["source_record_index"] = source_index
        row["payload_bytes"] = len(payload)
        rows.append(row)
        total_bytes += len(payload)
        total_gaussians += int(row["num_gaussians"])

    ranked, scores, definition = _ranking(rows, importance)
    prefix_bytes = [0]
    for index in ranked:
        prefix_bytes.append(prefix_bytes[-1] + int(rows[index]["payload_bytes"]))

    target_bytes = total_bytes * l4s_fraction
    if l4s_fraction == 0.0:
        high_count = 0
    elif l4s_fraction == 1.0:
        high_count = len(rows)
    else:
        # Interior fractions keep both paths non-empty. Pick the rank-prefix
        # whose byte volume is closest to the requested L4S fraction.
        high_count = min(
            range(1, len(rows)),
            key=lambda count: (abs(prefix_bytes[count] - target_bytes), count),
        )
    high_indices = set(ranked[:high_count])
    low_indices = set(range(len(rows))) - high_indices

    def _selected(indices: set[int]) -> Iterable[bytes]:
        for index, payload in enumerate(payloads):
            if index in indices:
                yield payload

    high_path = output_dir / "high-priority.bundle"
    low_path = output_dir / "low-priority.bundle"
    high_disk = write_bundle(high_path, _selected(high_indices))
    low_disk = write_bundle(low_path, _selected(low_indices))

    rank_position = {source_index: rank for rank, source_index in enumerate(ranked)}
    manifest_rows = []
    high_gaussians = 0
    low_gaussians = 0
    for index, row in enumerate(rows):
        high = index in high_indices
        gaussians = int(row["num_gaussians"])
        if high:
            high_gaussians += gaussians
        else:
            low_gaussians += gaussians
        manifest_rows.append(
            {
                **row,
                "importance_score": scores[index],
                "importance_rank": rank_position[index],
                "priority": "high" if high else "low",
                "transport": "prague" if high else "reno",
            }
        )

    actual_fraction = high_disk["payload_bytes"] / total_bytes
    manifest: dict[str, object] = {
        "schema_version": 1,
        "source_bundle": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "source_objects": len(rows),
        "source_payload_bytes": total_bytes,
        "source_gaussians": total_gaussians,
        "importance": importance,
        "importance_definition": definition,
        "split_rule": (
            "ranked prefix nearest requested payload-byte fraction -> Prague/L4S; "
            "remainder -> Reno/Classic; endpoints assign the complete scene "
            "to one transport"
        ),
        "requested_l4s_byte_fraction": l4s_fraction,
        "actual_l4s_byte_fraction": actual_fraction,
        "fraction_error": actual_fraction - l4s_fraction,
        "object_boundaries_preserved": True,
        "source_order_preserved_within_each_output": True,
        "high": {
            **high_disk,
            "gaussians": high_gaussians,
            "sha256": sha256_file(high_path),
            "path": str(high_path),
            "transport": "prague",
        },
        "low": {
            **low_disk,
            "gaussians": low_gaussians,
            "sha256": sha256_file(low_path),
            "path": str(low_path),
            "transport": "reno",
        },
        "balance": {
            "requested_l4s_byte_fraction": l4s_fraction,
            "actual_l4s_byte_fraction": actual_fraction,
            "object_fraction_high": high_disk["objects"] / len(rows),
            "gaussian_fraction_high": high_gaussians / total_gaussians,
        },
        "objects": manifest_rows,
    }
    (output_dir / "priority-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def derive_first_visible_object_order(
    source_bundle: Path,
    trace_path: Path,
    dependency: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    frame_stride: int = 1,
    start_time_ms: float = 0.0,
    wrap_end_time_ms: float | None = None,
    fov_scale: float = 1.0,
    allow_unpinned: bool = False,
) -> dict[str, object]:
    """Derive first-visible timestamps for every encoded object.

    This is intentionally a *pre-experiment* step. The network run consumes the
    resulting ordered list and never consults camera state, so every repetition
    has identical demand ordering.
    """
    if frame_stride <= 0:
        raise ValueError("frame_stride must be positive")
    if start_time_ms < 0 or not 0 < fov_scale <= 1:
        raise ValueError("start time must be non-negative and fov scale must be in (0, 1]")
    require_dependency(dependency, allow_unpinned=allow_unpinned)
    _activate_dependency(dependency)

    import numpy as np  # type: ignore
    # Importing streaming.transport.client executes the live MoQ client package
    # initializer and loads libmoq.so. Demand freezing only needs the pure
    # viewport math, so load that source module without importing its parent.
    frustum = runpy.run_path(
        str(
            Path(dependency)
            / "src/streaming/transport/client/viewport/frustum.py"
        )
    )
    check_aabb_frustum = frustum["check_aabb_frustum"]
    extract_frustum = frustum["extract_frustum"]
    projection_matrix_from_fov = frustum["projection_matrix_from_fov"]
    scene_far_from_bboxes = frustum["scene_far_from_bboxes"]

    payloads = list(read_bundle(source_bundle))
    if not payloads:
        raise RuntimeError(f"{source_bundle}: bundle has no objects")
    bounds: list[tuple[tuple[object, ...], object, object, float]] = []
    for payload in payloads:
        identity = object_identity(payload)
        importance = object_importance(payload)
        arrays = _array_views(payload, identity)
        means = arrays["means"]
        if means is None:
            raise ValueError("object visibility requires Gaussian means")
        values = list(_float_values(means, int(identity["num_gaussians"]) * 3, "means"))
        points = np.asarray(values, dtype=np.float64).reshape(-1, 3)
        bounds.append((_object_key(importance), points.min(axis=0), points.max(axis=0), len(payload)))
    scene_mins = [item[1] for item in bounds]
    scene_maxs = [item[2] for item in bounds]
    frames = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    if isinstance(frames, dict) and "frames" in frames:
        frames = frames["frames"]
    if not frames:
        raise RuntimeError(f"{trace_path}: trace has no frames")
    if start_time_ms or wrap_end_time_ms is not None:
        start_index = next(
            (index for index, frame in enumerate(frames)
             if float(frame["timestamp_ms"]) >= start_time_ms),
            len(frames),
        )
        if start_index == len(frames):
            raise ValueError("start time is beyond the trace")
        prefix = [
            frame for frame in frames[:start_index]
            if wrap_end_time_ms is None or float(frame["timestamp_ms"]) <= wrap_end_time_ms
        ]
        frames = frames[start_index:] + prefix
        base_time = float(frames[0]["timestamp_ms"])
        trace_duration = float(frames[-1]["timestamp_ms"]) - base_time
        frames = [
            {**frame,
             "timestamp_ms": (
                 float(frame["timestamp_ms"]) - base_time
                 if index < len(frames) - len(prefix)
                 else float(frame["timestamp_ms"]) + trace_duration - float(frames[0]["timestamp_ms"])
             ),
             "fov": float(frame["fov"]) * fov_scale}
            for index, frame in enumerate(frames)
        ]
    elif fov_scale != 1.0:
        frames = [{**frame, "fov": float(frame["fov"]) * fov_scale} for frame in frames]

    aspect = width / max(height, 1)
    scene_far = scene_far_from_bboxes(scene_mins, scene_maxs)
    seen: set[str] = set()
    events: list[dict[str, object]] = []

    for frame_index in range(0, len(frames), frame_stride):
        frame = frames[frame_index]
        view = np.asarray(frame["view_matrix"], dtype=np.float64)
        projection = projection_matrix_from_fov(
            float(frame["fov"]), aspect=aspect, far=scene_far
        )
        frustum = extract_frustum(projection @ view)
        camera = np.asarray(frame["camera_position"], dtype=np.float64)
        for key, bbox_min, bbox_max, _ in bounds:
            key_text = json.dumps(list(key), separators=(",", ":"))
            if key_text in seen:
                continue
            if check_aabb_frustum(bbox_min, bbox_max, frustum) == 0:
                continue
            centroid = (bbox_min + bbox_max) / 2.0
            seen.add(key_text)
            events.append({
                "object_key": list(key),
                "frame_index": frame_index,
                "timestamp_ms": float(frame["timestamp_ms"]),
                "distance": float(np.linalg.norm(camera - centroid)),
            })

    never_visible = [list(item[0]) for item in bounds
                     if json.dumps(list(item[0]), separators=(",", ":")) not in seen]
    return {
        "schema_version": 2,
        "eligibility_granularity": "object-aabb",
        "source_bundle": str(Path(source_bundle).resolve()),
        "source_bundle_sha256": sha256_file(Path(source_bundle)),
        "trace": str(Path(trace_path).resolve()),
        "trace_sha256": sha256_file(Path(trace_path)),
        "frame_stride": frame_stride,
        "width": width,
        "height": height,
        "events": events,
        "object_order": [event["object_key"] for event in events] + never_visible,
        "never_visible": never_visible,
        "initial_visibility_spread_ms": 10000.0,
        "trace_start_time_ms": start_time_ms,
        "trace_wrap_end_time_ms": wrap_end_time_ms,
        "fov_scale": fov_scale,
    }


def _key_json(key: tuple[object, ...]) -> str:
    return json.dumps(list(key), separators=(",", ":"))


def object_release_times(
    frozen_demand: dict[str, object],
    *,
    initial_release_ms: float,
    time_scale: float,
    initial_visibility_spread_ms: float,
) -> dict[tuple[object, ...], int]:
    events = list(frozen_demand.get("events", []))
    initial = [event for event in events if float(event["timestamp_ms"]) == 0.0]
    initial.sort(key=lambda event: (float(event.get("distance", 0.0)), _key_json(tuple(event["object_key"]))))
    releases: dict[tuple[object, ...], int] = {}
    count = len(initial)
    for index, event in enumerate(initial):
        key = tuple(event["object_key"])
        offset = initial_visibility_spread_ms * index / max(count, 1)
        releases[key] = int(round(initial_release_ms + offset))
    for event in events:
        key = tuple(event["object_key"])
        if key not in releases:
            releases[key] = int(round(initial_release_ms + float(event["timestamp_ms"]) * time_scale))
    return releases


def reorder_bundle_by_object_order(
    source: Path,
    destination: Path,
    release_times: Mapping[tuple[object, ...], int],
) -> dict[str, object]:
    """Write visible objects in deterministic release-time order."""
    records: list[tuple[int, tuple[object, ...], int, bytes]] = []
    unknown: set[str] = set()
    for source_index, payload in enumerate(read_bundle(source)):
        key = _object_key_from_payload(payload)
        if key not in release_times:
            unknown.add(_key_json(key))
            continue
        records.append((release_times[key], key, source_index, payload))
    records.sort(key=lambda item: (item[0], _key_json(item[1]), item[2]))
    summary = write_bundle(destination, (item[3] for item in records))
    return {
        **summary,
        "sha256": sha256_file(destination),
        "object_order": [_key_json(item[1]) for item in records],
        "unknown_tracks": sorted(unknown),
    }


def write_trace_release_schedule(
    bundle_path: Path,
    schedule_path: Path,
    *,
    frozen_demand: dict[str, object],
    initial_release_ms: float = 0.0,
    time_scale: float = 1.0,
    fallback_spacing_ms: float = 100.0,
    importance_ranks: Mapping[tuple[object, ...], int],
) -> dict[str, object]:
    """Record viewport eligibility and global rank for every bundle object.

    Base and Enhancement objects become eligible at their object's frozen
    first-visible time. The resulting per-record
    ``release_ms importance_rank`` schedule is fixed on disk and reused
    verbatim. The publisher chooses the lowest rank among all eligible records;
    file order is not admission order.
    """
    if initial_release_ms < 0 or time_scale <= 0 or fallback_spacing_ms < 0:
        raise ValueError("invalid trace release timing")
    release_times = object_release_times(
        frozen_demand,
        initial_release_ms=initial_release_ms,
        time_scale=time_scale,
        initial_visibility_spread_ms=float(
            frozen_demand.get("initial_visibility_spread_ms", 10000.0)
        ),
    )

    records: list[tuple[int, int, bytes, tuple[object, ...]]] = []
    for source_index, payload in enumerate(read_bundle(bundle_path)):
        row = object_importance(payload)
        key = _object_key(row)
        if key not in release_times:
            continue
        records.append((release_times[key], source_index, payload, key))
    # The caller normally passes an already release-ordered bundle; this check is
    # deliberately strict so a schedule can never silently refer to the wrong record.
    if records != sorted(records, key=lambda item: (item[0], item[1])):
        raise ValueError(
            "bundle order is inconsistent with object release order; "
            "call reorder_bundle_by_object_order first"
        )

    release_rows = [int(release_time) for release_time, _, _, _ in records]
    if release_rows != sorted(release_rows):
        raise ValueError("trace-derived release times are not non-decreasing")
    rank_rows = [
        _rank_for_payload(payload, importance_ranks)
        for _, _, payload, _ in records
    ]
    if len(set(rank_rows)) != len(rank_rows):
        raise ValueError("bundle schedule contains duplicate importance ranks")

    schedule_path = Path(schedule_path)
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    schedule_path.write_text(
        "".join(
            f"{release_ms} {importance_rank}\n"
            for release_ms, importance_rank in zip(release_rows, rank_rows)
        ),
        encoding="utf-8",
    )
    return {
        "path": str(schedule_path),
        "sha256": sha256_file(schedule_path),
        "records": len(release_rows),
        "format": "release_ms importance_rank",
        "admission_policy": "lowest importance rank among currently eligible records",
        "timing": "object AABB first-visible timestamps with paced frame-zero objects",
        "viewport_gated_subgroups": [0, 1, 2],
        "immediately_eligible_subgroups": [],
        "initial_release_ms": initial_release_ms,
        "time_scale": time_scale,
        "fallback_spacing_ms": fallback_spacing_ms,
        "release_by_object": {_key_json(key): release for key, release in release_times.items()},
    }


def _read_release_schedule(schedule_path: Path) -> list[tuple[int, int]]:
    schedule: list[tuple[int, int]] = []
    for line_number, line in enumerate(
        Path(schedule_path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        fields = line.split()
        if len(fields) != 2:
            raise ValueError(f"{schedule_path}:{line_number}: invalid schedule row")
        release_ms, importance_rank = map(int, fields)
        if release_ms < 0 or importance_rank < 0:
            raise ValueError(f"{schedule_path}:{line_number}: negative schedule value")
        schedule.append((release_ms, importance_rank))
    return schedule


def validate_admission_order(
    schedule_path: Path,
    admission_path: Path,
) -> dict[str, object]:
    """Prove every logged admission selected the best currently eligible rank."""
    schedule = _read_release_schedule(schedule_path)

    admitted: set[int] = set()
    with Path(admission_path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    required = {
        "admission_index", "time_us", "bundle_record_index", "release_ms",
        "importance_rank", "subgroup_id", "payload_bytes",
    }
    if set(reader.fieldnames or ()) != required:
        raise ValueError(f"{admission_path}: invalid admission log header")
    if not rows:
        return {
            "validated": True,
            "admission_policy": "lowest importance rank among currently eligible records",
            "scheduled_records": len(schedule),
            "admitted_records": 0,
        }

    for expected_index, row in enumerate(rows):
        admission_index = int(row["admission_index"])
        time_us = int(row["time_us"])
        record_index = int(row["bundle_record_index"])
        release_ms = int(row["release_ms"])
        importance_rank = int(row["importance_rank"])
        if admission_index != expected_index:
            raise ValueError(f"{admission_path}: non-sequential admission index")
        if not 0 <= record_index < len(schedule) or record_index in admitted:
            raise ValueError(f"{admission_path}: invalid repeated bundle record index")
        if schedule[record_index] != (release_ms, importance_rank):
            raise ValueError(f"{admission_path}: admission does not match schedule")
        elapsed_ms = time_us // 1000
        eligible = [
            (rank, index)
            for index, (release, rank) in enumerate(schedule)
            if index not in admitted and release <= elapsed_ms
        ]
        if not eligible:
            raise ValueError(f"{admission_path}: object admitted before eligibility")
        expected_rank, expected_record = min(eligible)
        if (importance_rank, record_index) != (expected_rank, expected_record):
            raise ValueError(
                f"{admission_path}: rank {importance_rank} record {record_index} "
                f"admitted instead of eligible rank {expected_rank} "
                f"record {expected_record}"
            )
        admitted.add(record_index)

    return {
        "validated": True,
        "admission_policy": "lowest importance rank among currently eligible records",
        "scheduled_records": len(schedule),
        "admitted_records": len(admitted),
    }


def validate_cross_path_admission_order(
    prague_schedule_path: Path,
    classic_schedule_path: Path,
    admission_path: Path,
) -> dict[str, object]:
    """Prove that Classic was admitted only while Prague had no eligible object."""

    schedules = {
        "high-prague": _read_release_schedule(prague_schedule_path),
        "low-reno": _read_release_schedule(classic_schedule_path),
    }
    admitted = {name: set() for name in schedules}
    rows = []
    with Path(admission_path).open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {
            "admission_index", "time_us", "path", "bundle_record_index",
            "release_ms", "importance_rank", "subgroup_id", "payload_bytes",
            "queued_stream_bytes_before", "bytes_in_flight_before",
            "cwnd_bytes_before", "queue_threshold_bytes",
        }
        if set(reader.fieldnames or ()) != required:
            raise ValueError(f"{admission_path}: invalid combined admission header")
        for expected_index, row in enumerate(reader):
            index = int(row["admission_index"])
            if index != expected_index:
                raise ValueError(f"{admission_path}: non-contiguous global admission index")
            path = row["path"]
            if path not in schedules:
                raise ValueError(f"{admission_path}: unknown admission path {path!r}")
            record_index = int(row["bundle_record_index"])
            time_us = int(row["time_us"])
            schedule = schedules[path]
            if record_index < 0 or record_index >= len(schedule):
                raise ValueError(f"{admission_path}: invalid {path} record index")
            if record_index in admitted[path]:
                raise ValueError(f"{admission_path}: duplicate {path} admission")
            release_ms, importance_rank = schedule[record_index]
            if (release_ms, importance_rank) != (
                int(row["release_ms"]),
                int(row["importance_rank"]),
            ):
                raise ValueError(f"{admission_path}: admission does not match schedule")
            if time_us < release_ms * 1000:
                raise ValueError(f"{admission_path}: object admitted before release")
            eligible_prague = [
                candidate
                for candidate, (release, _) in enumerate(schedules["high-prague"])
                if candidate not in admitted["high-prague"]
                and release * 1000 <= time_us
            ]
            if path == "low-reno" and eligible_prague:
                raise ValueError(
                    f"{admission_path}: Classic admitted while Prague was available"
                )
            admitted[path].add(record_index)
            rows.append(row)

    return {
        "validated": True,
        "admitted_records": len(rows),
        "prague_records": len(schedules["high-prague"]),
        "classic_records": len(schedules["low-reno"]),
        "admitted_prague_records": len(admitted["high-prague"]),
        "admitted_classic_records": len(admitted["low-reno"]),
        "admission_policy": "Prague-first high-biased two-queue select",
    }
