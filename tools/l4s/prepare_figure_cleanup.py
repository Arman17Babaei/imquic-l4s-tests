#!/usr/bin/env python3
"""Inventory and safely remove scratch evidence superseded by Figures 6--15 inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProtectedFamily:
    pattern: str
    reason: str


PROTECTED_FAMILIES = (
    ProtectedFamily("results/records/moq-8s-unlimited-cc-matrix-20260817", "figures-6-8-baseline"),
    ProtectedFamily("results/l4s/3dgs-preparation", "shared-input"),
    ProtectedFamily("results/l4s/3dgs-reordering-demand", "shared-input"),
    ProtectedFamily("results/l4s/qemu-3dgs-l4s-classic-step5-table-20260821T111820Z", "figures-12-14"),
    ProtectedFamily("results/l4s/qemu-3dgs-l4s-l4s-step5-table-20260821T112935Z", "figures-12-14"),
    ProtectedFamily("results/l4s/qemu-3dgs-split2-*", "figure-12"),
    ProtectedFamily("results/l4s/qemu-3dgs-priority-split-*", "figure-12"),
    ProtectedFamily("results/l4s/qemu-3dgs-trials-*", "figure-12"),
    ProtectedFamily("results/l4s/qemu-3dgs-isolated-server-l4s-20260822-20260822T035736Z", "figure-12"),
    ProtectedFamily("results/l4s/qemu-3dgs-isolated-client-classic-20260822-20260822T040218Z", "figure-12"),
    ProtectedFamily("results/l4s/qemu-3dgs-isolated-client-dualpi2-20260822-20260822T040555Z", "figure-12"),
    ProtectedFamily("results/l4s/qemu-3dgs-reno-cwnd256804-*", "figures-13-14"),
    ProtectedFamily("results/l4s/qemu-3dgs-prague-first-*", "figures-13-14"),
    ProtectedFamily("results/l4s/qemu-3dgs-cyclic-fov50-*", "figure-15"),
    ProtectedFamily("results/l4s/qemu-3dgs-reordering-eligible-rank-viewport-*", "figure-15"),
    ProtectedFamily("results/records/3dgs-reordering-*", "figure-15"),
    ProtectedFamily("results/records/qemu-thesis-network-figures-06-08-*", "figures-6-8-new-evidence"),
    ProtectedFamily("results/l4s/qemu-thesis-3dgs-pair-rep-01-final-20260825T164914Z", "figures-9-10-validated"),
    ProtectedFamily("results/l4s/qemu-thesis-3dgs-pair-rep-02-rerun3-final-20260825T182350Z", "figures-9-10-validated"),
    ProtectedFamily("results/l4s/qemu-thesis-3dgs-pair-rep-03-rerun-final-20260825T195325Z", "figures-9-10-validated"),
    ProtectedFamily("results/figures/thesis-figures-06-10", "figures-6-10-artifacts"),
    ProtectedFamily("results/l4s/comparisons", "figures-6-15-comparisons"),
    ProtectedFamily("results/venvs", "shared-environment"),
    ProtectedFamily("results/downloads", "shared-input"),
    ProtectedFamily("fonts", "persian-fonts"),
    ProtectedFamily("data", "shared-input"),
    ProtectedFamily("deps", "source-dependency"),
)


INITIAL_DELETION_CANDIDATES = (
    "results/l4s/qemu-3dgs-l4s-classic-step5-table-20260821T103029Z",
    "results/l4s/qemu-3dgs-l4s-classic-step5-table-20260821T104045Z",
    "results/l4s/qemu-3dgs-l4s-classic-step5-table-final-20260821T105957Z",
    "results/l4s/qemu-3dgs-reordering-20260820T183521Z",
    "results/l4s/qemu-3dgs-reordering-20260820T183820Z",
    "results/l4s/qemu-3dgs-reordering-20260820T184050Z",
    "results/3dgs/qemu",
    "results/l4s/qemu-sustained-moq-20260806T154253Z",
    "results/l4s/qemu-sustained-moq-20260806T160102Z",
    "results/l4s/qemu-sustained-moq-20260806T171614Z",
    "results/l4s/qemu-sustained-moq-20260806T192603Z",
    "results/l4s/qemu-sustained-moq-20260806T194939Z",
    "results/l4s/qemu-sustained-moq-20260806T202339Z",
    "results/l4s/qemu-sustained-moq-20260806T205950Z",
    "results/l4s/qemu-3dgs-l4s-classic-step5-lifecycle-diagnostic-20260821T105146Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-step15-20260822-20260822T045322Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-step10-20260822-20260822T044937Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-target20-update32-20260822-20260822T050311Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-50m-zero-rtt-20260822-20260822T054015Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-50m-rtt20-retry-20260822-20260822T054852Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-50m-rtt20-warm10-step10-20260822-20260822T064925Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-50m-rtt20-warm10-20260822-20260822T063151Z",
    "results/l4s/qemu-3dgs-isolated-client-dualpi2-recorded20-20260822-20260822T052732Z",
    "results/l4s/p0-reference-20260807T085316Z",
    "results/l4s/qemu-mininet-bbr-20260813T090510Z",
    "results/l4s/qemu-mininet-final-20260806T090027Z",
    "results/l4s/qemu-dualpi2-reference-20260817T065306Z",
    "results/records/3dgs-l4s-profile-bg100-20260817",
    "results/records/3dgs-l4s-profile-bg50-20260817",
    "results/records/3dgs-bg0-reno-prague-20260817",
    "results/records/3dgs-bg50-reno-prague-20260817",
    "results/records/3dgs-bg100-reno-prague-20260817",
    "results/l4s/qemu-3dgs-l4s-classic-one-20260821T062051Z",
    "results/l4s/qemu-3dgs-l4s-classic-one-20260821T061214Z",
    "results/l4s/qemu-3dgs-l4s-l4s-one-20260821T062649Z",
    "results/l4s/qemu-3dgs-full-l4s-classic-20260821-20260821T161041Z",
    "results/l4s/qemu-3dgs-full-l4s-dualpi2-20260821-20260821T161348Z",
    "results/l4s/qemu-3dgs-reordering-two-l4s-all-l4s-20260820T202244Z",
    "results/l4s/qemu-3dgs-reordering-eligible-rank-all-l4s-20260821T033515Z",
    "results/l4s/qemu-3dgs-l4s-classic-step5-isolated-table-20260821T111130Z",
    "results/l4s/qemu-reno-fairness-20260809T070818Z",
    "results/l4s/qemu-mininet-bbrv1-reno-ect-prague-20260817T074552Z",
    "results/l4s/qemu-reno-fairness-20260809T055658Z",
    "results/l4s/p2-duration-validation-20260807T134303Z",
    "results/l4s/qemu-thesis-3dgs-pair-rep-02-final-20260825T181113Z",
    "results/l4s/qemu-thesis-3dgs-pair-rep-02-rerun-final-20260825T181528Z",
    "results/l4s/qemu-thesis-3dgs-pair-rep-02-rerun2-final-20260825T181949Z",
    "results/l4s/qemu-thesis-3dgs-pair-rep-03-final-20260825T194449Z",
)

OUTPUT_ONLY_REFERENCES = {
    "results/3dgs/qemu": {"tools/l4s/run_qemu_3dgs_native.py"},
}


def contained(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or resolved == ROOT:
        raise ValueError(f"unsafe path outside workspace: {path}")
    return resolved


def tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix().encode()
        file_digest = hashlib.sha256()
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                file_digest.update(chunk)
        digest.update(relative + b"\0" + str(item.stat().st_size).encode() + b"\0")
        digest.update(file_digest.digest())
    return digest.hexdigest()


def protected_entries(previous: dict[str, object] | None = None) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    previous_rows = {
        str(row.get("path")): row for row in (previous or {}).get("protected", [])
    }
    for family in PROTECTED_FAMILIES:
        for path in sorted(ROOT.glob(family.pattern)):
            if not path.exists():
                continue
            relative = str(contained(path).relative_to(ROOT))
            size = tree_size(path) if path.is_dir() else path.stat().st_size
            previous_row = previous_rows.get(relative, {})
            digest = (
                previous_row.get("content_manifest_sha256")
                if previous_row.get("bytes") == size else None
            )
            entries.append({
                "path": relative,
                "reason": family.reason,
                "bytes": size,
                "content_manifest_sha256": digest or (
                    tree_digest(path) if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
                ),
            })
    return entries


def searchable_protected_files(protected: list[dict[str, object]]) -> list[Path]:
    files = list(ROOT.glob("results/*.py")) + [
        path for path in (ROOT / "tools/l4s").glob("*.py")
        if path.resolve() != Path(__file__).resolve()
    ]
    for entry in protected:
        path = ROOT / str(entry["path"])
        if path.is_file() and path.suffix in {".json", ".md", ".py", ".csv"}:
            files.append(path)
        elif path.is_dir():
            for suffix in ("*.json", "*.md", "*.py"):
                files.extend(path.rglob(suffix))
    return sorted(set(files))


def references_to(candidate: str, files: list[Path]) -> list[str]:
    references: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if candidate in text:
            references.append(str(path.relative_to(ROOT)))
    return references


def build_manifest(previous: dict[str, object] | None = None) -> dict[str, object]:
    protected = protected_entries(previous)
    protected_paths = {str(entry["path"]) for entry in protected}
    searchable = searchable_protected_files(protected)
    previous_rows = {
        str(row.get("path")): row for row in (previous or {}).get("candidates", [])
    }
    candidates: list[dict[str, object]] = []
    for relative in INITIAL_DELETION_CANDIDATES:
        path = contained(ROOT / relative)
        if not path.exists():
            candidates.append({"path": relative, "classification": "already-absent"})
            continue
        overlaps = sorted(
            protected_path for protected_path in protected_paths
            if path == ROOT / protected_path
            or path.is_relative_to(ROOT / protected_path)
            or (ROOT / protected_path).is_relative_to(path)
        )
        references = references_to(relative, searchable)
        references = [
            reference for reference in references
            if reference not in OUTPUT_ONLY_REFERENCES.get(relative, set())
        ]
        classification = "blocked" if overlaps or references else "superseded"
        previous_row = previous_rows.get(relative, {})
        size = tree_size(path)
        digest = (
            previous_row.get("content_manifest_sha256")
            if previous_row.get("bytes") == size else None
        )
        candidates.append({
            "path": relative,
            "resolved_path": str(path),
            "bytes": size,
            "content_manifest_sha256": digest or tree_digest(path),
            "provenance_files": [
                str(item.relative_to(ROOT)) for item in sorted(path.rglob("provenance.json"))
            ],
            "protected_overlaps": overlaps,
            "protected_references": references,
            "classification": classification,
            "reason": "incomplete-or-superseded-scratch-evidence",
        })
    return {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "workspace": str(ROOT),
        "protected": protected,
        "candidates": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--delete-approved", action="store_true")
    args = parser.parse_args()
    previous = None
    if args.output.is_file():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
    manifest = build_manifest(previous)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    blocked = [row for row in manifest["candidates"] if row["classification"] == "blocked"]
    if blocked:
        print(f"retaining {len(blocked)} referenced/protected candidates")
    if args.delete_approved:
        for row in manifest["candidates"]:
            if row["classification"] != "superseded":
                continue
            path = contained(ROOT / str(row["path"]))
            shutil.rmtree(path)
            row["deleted_utc"] = datetime.now(timezone.utc).isoformat()
        args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
