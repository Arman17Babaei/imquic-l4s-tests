#!/usr/bin/env python3
"""Validate a live IMQUIC Prague metrics trajectory."""

import argparse
import csv
import json
from pathlib import Path


FIELDS = (
    "time_us",
    "rtt_us",
    "cwnd_bytes",
    "bytes_in_flight",
    "pacing_Bps",
    "ect1_packets",
    "ce_packets",
    "alpha_numerator",
    "alpha_denominator",
)


class AnalysisError(ValueError):
    pass


def load_samples(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise AnalysisError("unexpected CSV header")
        try:
            return [{field: int(row[field]) for field in FIELDS} for row in reader]
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalysisError(f"invalid numeric sample: {exc}") from exc


def nondecreasing(samples, field):
    return all(a[field] <= b[field] for a, b in zip(samples, samples[1:]))


def analyze(samples):
    if len(samples) < 10:
        raise AnalysisError(f"need at least 10 samples, got {len(samples)}")
    if not all(a["time_us"] < b["time_us"] for a, b in zip(samples, samples[1:])):
        raise AnalysisError("timestamps are not strictly increasing")
    for field in ("ect1_packets", "ce_packets"):
        if not nondecreasing(samples, field):
            raise AnalysisError(f"{field} decreased")
    if samples[-1]["ect1_packets"] == 0:
        raise AnalysisError("no ECT(1) feedback was observed")
    if samples[-1]["ce_packets"] == 0:
        raise AnalysisError("no CE feedback was observed")

    valid = [sample for sample in samples if sample["cwnd_bytes"] > 0]
    alpha_values = {
        (sample["alpha_numerator"], sample["alpha_denominator"])
        for sample in valid
        if sample["alpha_denominator"] > 0
    }
    cwnd_values = {sample["cwnd_bytes"] for sample in valid}
    if len(alpha_values) < 2:
        raise AnalysisError("Prague alpha did not evolve")
    if len(cwnd_values) < 2:
        raise AnalysisError("congestion window did not evolve")

    ce_reduction = False
    maximum_before = 0
    for index, sample in enumerate(valid):
        if index > 0 and sample["ce_packets"] > valid[index - 1]["ce_packets"]:
            following = valid[index : index + 6]
            if maximum_before and any(
                later["cwnd_bytes"] < maximum_before for later in following
            ):
                ce_reduction = True
                break
        maximum_before = max(maximum_before, sample["cwnd_bytes"])
    if not ce_reduction:
        raise AnalysisError("no CE-associated congestion-window reduction was observed")

    return {
        "status": "pass",
        "samples": len(samples),
        "duration_us": samples[-1]["time_us"] - samples[0]["time_us"],
        "final_ect1_packets": samples[-1]["ect1_packets"],
        "final_ce_packets": samples[-1]["ce_packets"],
        "alpha_states": len(alpha_values),
        "cwnd_min_bytes": min(cwnd_values),
        "cwnd_max_bytes": max(cwnd_values),
        "ce_associated_cwnd_reduction": True,
    }


def self_test():
    rows = []
    for index in range(12):
        rows.append(
            {
                "time_us": index * 10000,
                "rtt_us": 10000,
                "cwnd_bytes": 12000 + index * 1000 if index < 6 else 9000 + index * 100,
                "bytes_in_flight": 8000,
                "pacing_Bps": 1000000,
                "ect1_packets": index * 4,
                "ce_packets": 0 if index < 6 else index - 5,
                "alpha_numerator": 0 if index < 6 else index - 5,
                "alpha_denominator": 1 if index < 6 else 16,
            }
        )
    result = analyze(rows)
    if result["status"] != "pass":
        raise AnalysisError("positive self-test did not pass")
    broken = [dict(row) for row in rows]
    for row in broken:
        row["ce_packets"] = 0
    try:
        analyze(broken)
    except AnalysisError:
        return
    raise AnalysisError("negative self-test unexpectedly passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="?", help="IMQUIC metrics CSV")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("Prague time-series analyzer self-test: PASS")
        return
    if not args.csv:
        parser.error("CSV is required unless --self-test is used")
    try:
        result = analyze(load_samples(args.csv))
    except AnalysisError as exc:
        raise SystemExit(f"Prague time-series analysis: FAIL: {exc}") from exc
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
