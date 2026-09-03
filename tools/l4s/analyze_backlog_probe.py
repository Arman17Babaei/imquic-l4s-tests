#!/usr/bin/env python3
"""Reduce controlled backlog-probe QEMU records to plot-ready CSV."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def one_case(case: Path, event_ms: float, scheme: str, backlog_bdp: float, lead_rtt: float) -> dict:
    high = case / "high-prague"
    arrival = read(high / "arrival-timeline.csv")
    if not arrival:
        raise ValueError(f"{case}: important object did not arrive")
    first = float(arrival[0].get("arrival_time_us", arrival[0].get("experiment_time_us"))) / 1000
    admission = read(high / "admission-order.csv")
    arrivals = {int(row["bundle_record_index"]): float(row.get("arrival_time_us", row.get("experiment_time_us", "inf"))) / 1000
                 for row in arrival}
    residual = 0
    for row in admission:
        if float(row["time_us"]) / 1000 <= event_ms and arrivals.get(int(row["bundle_record_index"]), float("inf")) > event_ms:
            residual += int(row["payload_bytes"])
    return {"scheme": scheme, "backlog_bdp": backlog_bdp, "lead_rtt": lead_rtt,
            "event_ms": event_ms, "latency_ms": first - event_ms,
            "residual_bytes": residual, "gain_ms": 0.0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for result in sorted(args.root.glob("backlog-smoke-*-*")):
        summary = result / "summary.json"
        if not summary.is_file():
            continue
        for case in sorted(result.glob("l4s-*-rep-*")):
            name = case.name.split("-rep-")[0].removeprefix("l4s-")
            fraction = float(name.replace("p", "."))
            scheme = "single" if fraction == 1.0 else "differentiated"
            probe = None
            frozen = result / "inputs" / f"l4s-{name}" / "reordering-input.json"
            if frozen.is_file():
                data = json.loads(frozen.read_text())
                probe = data.get("probe")
            if probe is None:
                continue
            event_ms = float(probe["preload_ms"]) + float(probe["lead_ms"]) + 5.0
            total = float(probe["important_payload_bytes"] + probe["selected_low_payload_bytes"])
            rows.append(one_case(case, event_ms, scheme,
                                 float(probe["selected_low_payload_bytes"]) / 250000.0,
                                 float(probe["lead_ms"]) / 20.0))
    by_cell = {}
    for row in rows:
        by_cell.setdefault((row["backlog_bdp"], row["lead_rtt"]), {})[row["scheme"]] = row
    for cell in by_cell.values():
        if "single" in cell and "differentiated" in cell:
            cell["differentiated"]["gain_ms"] = cell["single"]["latency_ms"] - cell["differentiated"]["latency_ms"]
    rows = [row for cell in by_cell.values() for row in cell.values()]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("scheme", "backlog_bdp", "lead_rtt", "event_ms", "latency_ms", "residual_bytes", "gain_ms"))
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps({"rows": len(rows), "cells": len(by_cell)}, indent=2))


if __name__ == "__main__":
    main()
