#!/usr/bin/env python3
"""Prepare reproducible synthetic 3DGS backlog probes for Figures 13--14."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from split_3dgs_priority import object_importance
from three_dgs_bundle import read_bundle, sha256_file, write_bundle

PRELOAD_MS = 1.0

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--backlog-bytes", type=int, required=True)
    parser.add_argument("--lead-ms", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.backlog_bytes < 0 or args.lead_ms < 0:
        parser.error("backlog and lead must be non-negative")
    records = list(read_bundle(args.source_bundle))
    ranked = sorted(records, key=lambda p: (
        int(object_importance(p)["subgroup_id"]),
        -float(object_importance(p)["opacity_score"]),
    ))
    if len(ranked) < 2:
        raise SystemExit("source bundle must contain at least two distinct objects")
    important = ranked[0]
    low = []
    total = 0
    for payload in reversed(ranked[1:]):
        if low and total >= args.backlog_bytes:
            break
        low.append(payload)
        total += len(payload)
    if args.backlog_bytes and total < args.backlog_bytes:
        raise SystemExit("source bundle is too small for requested backlog")
    selected = [important, *low]
    args.output.mkdir(parents=True, exist_ok=True)
    bundle = args.output / "probe.bundle"
    write_bundle(bundle, selected)
    identities = []
    for index, payload in enumerate(selected):
        row = object_importance(payload)
        key = [row["track_id"], row["group_id"], int(row["subgroup_id"]), int(row["object_id"])]
        identities.append(key)
    frozen = {
        "schema_version": 2, "eligibility_granularity": "object-aabb",
        "object_order": identities,
        "events": [
            {"object_key": key, "timestamp_ms": (PRELOAD_MS + args.lead_ms if index == 0
                else (0.0 if args.backlog_bytes else 60000.0)),
             "frame_index": 0, "distance": 1.0}
            for index, key in enumerate(identities)
        ],
        "initial_base_release_ms": 0.0, "initial_visibility_spread_ms": 0.0,
        "demand_time_scale": 1.0, "source_bundle": str(bundle.resolve()),
        "source_bundle_sha256": sha256_file(bundle),
        "probe": {"important_payload_bytes": len(important),
                  "requested_backlog_bytes": args.backlog_bytes,
                  "selected_low_payload_bytes": total,
                  "lead_ms": args.lead_ms, "preload_ms": PRELOAD_MS},
    }
    (args.output / "frozen-demand-order.json").write_text(json.dumps(frozen, indent=2) + "\n")
    (args.output / "probe-manifest.json").write_text(json.dumps({
        "bundle": str(bundle), "frozen_demand": str(args.output / "frozen-demand-order.json"),
        "backlog_bytes": total, "important_bytes": len(important),
        "lead_ms": args.lead_ms, "source_sha256": sha256_file(args.source_bundle),
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
