#!/usr/bin/env python3
"""Eligibility-to-arrival validation and support gate for thesis Figure 9."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


PERCENTILES = (50.0, 95.0, 99.0, 99.9)


def percentile(values: list[float], pct: float) -> float:
    values = sorted(values)
    if not values:
        raise ValueError("empty latency sample")
    pos = (len(values) - 1) * pct / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] if lo == hi else values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _only_case(cell: Path) -> Path:
    cases = sorted(path for path in cell.iterdir() if path.is_dir()
                   and (path / "result.json").is_file())
    if len(cases) != 1:
        raise RuntimeError(f"{cell}: expected exactly one case, found {len(cases)}")
    return cases[0]


def latency_summary(cell: Path) -> dict:
    case = _only_case(cell)
    summary = json.loads((cell / "summary.json").read_text())["cases"][0]
    analysis = json.loads((cell / "reordering-analysis.json").read_text())["cases"][0]
    provenance = json.loads((cell / "provenance.json").read_text())
    input_manifest = json.loads(
        next((cell / "inputs").glob("l4s-*/reordering-input.json")).read_text()
    )
    ordered_identities = {
        path: list(input_manifest["path_inputs"][path]["ordered"]["object_order"])
        for path in ("high-prague", "low-reno")
    }
    admission = {}
    with (case / "combined-admission-order.csv").open(newline="") as stream:
        for row in csv.DictReader(stream):
            path = row["path"]
            bundle_index = int(row["bundle_record_index"])
            identity = tuple(json.loads(ordered_identities[path][bundle_index]))
            admission[(path, *identity)] = float(row["release_ms"])
    latencies = []
    arrival_count = 0
    with (case / "combined-arrival-timeline.csv").open(newline="") as stream:
        for row in csv.DictReader(stream):
            arrival_count += 1
            key = (
                row["path"], row["track_id"], row["group_id"],
                int(row["subgroup_id"]), int(row["object_id"]),
            )
            if key not in admission:
                raise RuntimeError(f"{case.name}: arrival lacks frozen admission key {key}")
            latencies.append(float(row["experiment_time_us"]) / 1000.0 - admission[key])
    assigned = sum(int(path["assigned_objects"]) for path in summary["paths"].values())
    received = sum(int(path["received_objects"]) for path in summary["paths"].values())
    admission_valid = bool(summary["cross_path_admission_validation"]["validated"])
    capture_valid = analysis["acceptance"]["status"] == "pass"
    method_valid = (
        received == arrival_count <= len(admission) <= assigned
        and admission_valid and capture_valid and all(value >= 0 for value in latencies)
    )
    result = {
        "cell": str(cell), "case": case.name,
        "frozen_demand_sha256": provenance["configuration"]["frozen_demand_sha256"],
        "assigned_objects": assigned, "received_objects": received,
        "arrival_records": arrival_count, "admission_records": len(admission),
        "not_admitted_by_deadline": assigned - len(admission),
        "admitted_not_completed_by_deadline": len(admission) - arrival_count,
        "observation_horizon_ms": 45000,
        "censoring_policy": (
            "latency ECDF contains receiver completions observed by the fixed "
            "45-second interactive deadline; missing objects remain reported "
            "as censored/missing renderer state"
        ),
        "admission_valid": admission_valid, "capture_valid": capture_valid,
        "method_valid": method_valid,
        "latency_ms": latencies,
        "percentiles_ms": {str(p): percentile(latencies, p) for p in PERCENTILES},
    }
    return result


def evaluate_pair(classic: Path, l4s: Path) -> dict:
    classic_result = latency_summary(classic)
    l4s_result = latency_summary(l4s)
    frozen_equal = classic_result["frozen_demand_sha256"] == l4s_result["frozen_demand_sha256"]
    method_valid = classic_result["method_valid"] and l4s_result["method_valid"] and frozen_equal
    checks = {
        "frozen_eligibility_equal": frozen_equal,
        "classic_method_valid": classic_result["method_valid"],
        "l4s_method_valid": l4s_result["method_valid"],
        "l4s_p95_lower": l4s_result["percentiles_ms"]["95.0"] < classic_result["percentiles_ms"]["95.0"],
        "l4s_p99_lower": l4s_result["percentiles_ms"]["99.0"] < classic_result["percentiles_ms"]["99.0"],
    }
    support = checks["l4s_p95_lower"] and checks["l4s_p99_lower"]
    result = {"classic": classic_result, "l4s": l4s_result,
              "checks": checks, "method_valid": method_valid,
              "supports_l4s_claim": support if method_valid else None}
    output = classic.parent / "delivery-latency-pair-support.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    for label, data in (("classic", classic_result), ("l4s", l4s_result)):
        with (classic.parent / f"{label}-delivery-latency.csv").open("w", newline="") as stream:
            writer = csv.writer(stream); writer.writerow(("eligibility_to_arrival_ms",))
            writer.writerows((value,) for value in data["latency_ms"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("classic", type=Path)
    parser.add_argument("l4s", type=Path)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()
    result = evaluate_pair(args.classic, args.l4s)
    print(json.dumps(result["checks"], indent=2))
    if args.gate and not result["method_valid"]:
        raise SystemExit(2)
    if args.gate and not result["supports_l4s_claim"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
