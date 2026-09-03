#!/usr/bin/env python3
"""Write the final Figures 6--15 cleanup and retention report."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path


DELETED_THIS_PHASE = (
    ("results/l4s/qemu-thesis-3dgs-pair-rep-02-final-20260825T181113Z", 1117438541),
    ("results/l4s/qemu-thesis-3dgs-pair-rep-02-rerun-final-20260825T181528Z", 1108913375),
    ("results/l4s/qemu-thesis-3dgs-pair-rep-02-rerun2-final-20260825T181949Z", 1136412903),
    ("results/l4s/qemu-thesis-3dgs-pair-rep-03-final-20260825T194449Z", 2137331115),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    args.manifest = args.manifest.resolve()
    args.output_json = args.output_json.resolve()
    args.output_md = args.output_md.resolve()
    manifest = json.loads(args.manifest.read_text())
    protected = manifest["protected"]
    blocked = {row["path"]: row for row in manifest["candidates"]
               if row["classification"] == "blocked"}
    inventory = []
    for parent_name in ("results/records", "results/l4s", "results/3dgs"):
        parent = root / parent_name
        if not parent.is_dir():
            continue
        for path in sorted(item for item in parent.iterdir() if item.is_dir()):
            relative = str(path.relative_to(root))
            matches = [row for row in protected if (
                relative == row["path"]
                or row["path"].startswith(relative + "/")
                or relative.startswith(row["path"] + "/")
            )]
            if matches:
                classification = "protected"
                reasons = sorted({row["reason"] for row in matches})
            elif relative in blocked:
                classification = "referenced"
                reasons = ["referenced-by-protected-comparison"]
            else:
                classification = "ambiguous"
                reasons = ["not-explicitly-approved-for-deletion"]
            inventory.append({"path": relative, "classification": classification,
                              "reasons": reasons})
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in protected:
        grouped[row["reason"]].append({
            "path": row["path"], "bytes": row["bytes"],
            "content_manifest_sha256": row["content_manifest_sha256"],
        })
    stat = os.statvfs(root)
    free_bytes = stat.f_bavail * stat.f_frsize
    result = {
        "manifest": str(args.manifest.relative_to(root)),
        "deleted_this_phase": [
            {"path": path, "bytes": size, "recoverable": False}
            for path, size in DELETED_THIS_PHASE
        ],
        "deleted_this_phase_bytes": sum(size for _, size in DELETED_THIS_PHASE),
        "prior_recorded_cleanup": {
            "first_two_exact_tranches_bytes": 32792037891,
            "additional_tranche": "approximately 5 GB; exact per-path history was not retained by the regenerated manifest",
        },
        "protected_by_reason": dict(sorted(grouped.items())),
        "remaining_inventory": inventory,
        "ambiguous_retained": [row["path"] for row in inventory
                               if row["classification"] == "ambiguous"],
        "referenced_retained": [row["path"] for row in inventory
                                if row["classification"] == "referenced"],
        "remaining_free_bytes": free_bytes,
        "remaining_free_gib": free_bytes / 1024 ** 3,
    }
    args.output_json.write_text(json.dumps(result, indent=2) + "\n")
    lines = [
        "# Final cleanup report for Figures 6--15", "",
        f"Manifest: `{result['manifest']}`", "",
        f"This phase deleted {result['deleted_this_phase_bytes']:,} bytes from four exact method-failure paths. These deletions are not recoverable without an external backup.", "",
        f"Remaining free space: {result['remaining_free_gib']:.2f} GiB ({free_bytes:,} bytes).", "",
        "## Deleted this phase", "",
    ]
    lines.extend(f"- `{row['path']}` - {row['bytes']:,} bytes"
                 for row in result["deleted_this_phase"])
    lines.extend(("", "## Retained protected evidence", ""))
    for reason, rows in result["protected_by_reason"].items():
        lines.append(f"### {reason}"); lines.append("")
        lines.extend(f"- `{row['path']}` - {row['bytes']:,} bytes - `{row['content_manifest_sha256']}`"
                     for row in rows)
        lines.append("")
    lines.extend(("## Comparison-referenced evidence retained", ""))
    lines.extend(f"- `{path}`" for path in result["referenced_retained"])
    lines.extend(("", "## Ambiguous evidence deliberately retained", ""))
    lines.extend(f"- `{path}`" for path in result["ambiguous_retained"])
    lines.extend(("", "Unclassified paths were retained because the revised plan forbids deleting ambiguous evidence.", ""))
    args.output_md.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
