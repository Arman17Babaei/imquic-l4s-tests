#!/usr/bin/env python3
"""Validate and summarize the camera-driven MOQT priority experiment."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def key(row: dict[str, Any]) -> tuple[str, int, int]:
    return str(row["tile_id"]), int(row["refinement"]), int(row["object_id"])


def expected_objects(manifest: dict[str, Any]) -> dict[tuple[str, int, int], dict[str, Any]]:
    return {(tile["id"], int(refinement["index"]), int(obj["id"])): obj
            for tile in manifest["tiles"] for refinement in tile["refinements"]
            for obj in refinement["objects"]}


def validate_object_coverage(manifest: dict[str, Any], rows: list[dict[str, str]]) -> None:
    expected = expected_objects(manifest)
    actual: dict[tuple[str, int, int], dict[str, str]] = {}
    for row in rows:
        row_key = key(row)
        if row_key in actual:
            raise ValueError(f"duplicate object delivery row {row_key}")
        actual[row_key] = row
    if set(actual) != set(expected):
        raise ValueError(f"manifest coverage mismatch missing={set(expected)-set(actual)} extra={set(actual)-set(expected)}")
    for row_key, row in actual.items():
        obj = expected[row_key]
        if int(row["payload_bytes"]) != int(obj["bytes"]):
            raise ValueError(f"byte mismatch for {row_key}")
        if row["sha256"] != obj["sha256"]:
            raise ValueError(f"manifest hash mismatch for {row_key}")
        if row["status"] == "delivered":
            if row["actual_sha256"] != obj["sha256"]:
                raise ValueError(f"delivered hash mismatch for {row_key}")
            if not row["request_id"] or not row["monotonic_us"]:
                raise ValueError(f"delivered object lacks request/time mapping {row_key}")
        elif row["status"] != "undelivered":
            raise ValueError(f"invalid terminal status for {row_key}")


def validate_history(deliveries: list[dict[str, str]], history: list[dict[str, str]]) -> None:
    grouped: dict[tuple[str, int, int], list[dict[str, str]]] = defaultdict(list)
    for row in history:
        grouped[key(row)].append(row)
    for delivery in deliveries:
        rows = grouped.get(key(delivery), [])
        terminals = [row for row in rows if row["kind"] == "terminal"]
        if len(terminals) != 1:
            raise ValueError(f"object {key(delivery)} has {len(terminals)} terminal histories")
        terminal = terminals[0]
        if terminal["status"] != delivery["status"] or terminal["priority"] != delivery["subscriber_priority"]:
            raise ValueError(f"terminal history mismatch for {key(delivery)}")
        timestamps = [int(row["monotonic_us"]) for row in rows]
        if timestamps != sorted(timestamps):
            raise ValueError(f"non-monotonic history for {key(delivery)}")


def median_band(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("cannot aggregate an empty metric")
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def validate_decisions(manifest: dict[str, Any], rows: list[dict[str, str]], epochs: int) -> None:
    expected_requests = {(tile["id"], int(refinement["index"]))
                         for tile in manifest["tiles"] for refinement in tile["refinements"]
                         }
    grouped: dict[int, set[tuple[str, int]]] = defaultdict(set)
    for row in rows:
        epoch = int(row["epoch"])
        item = (row["tile_id"], int(row["refinement"]))
        if item in grouped[epoch]:
            raise ValueError(f"duplicate priority decision epoch={epoch} item={item}")
        grouped[epoch].add(item)
        lod = int(row["refinement"])
        viewport = {"center": 0, "periphery": 20, "outside": 40}[row["viewport"]]
        depth = {"near": 0, "middle": 8, "far": 16}[row["depth"]]
        if int(row["priority"]) != 40 * lod + viewport + depth:
            raise ValueError("priority formula mismatch")
    if sorted(grouped) != list(range(epochs)):
        raise ValueError("priority epochs are incomplete or non-monotonic")
    if any(items != expected_requests for items in grouped.values()):
        raise ValueError("priority decision request coverage mismatch")


def validate_updates(rows: list[dict[str, str]], expected_requests: int) -> None:
    initial = [row for row in rows if row["state"] == "initial"]
    if len(initial) != expected_requests or len({row["request_id"] for row in initial}) != expected_requests:
        raise ValueError("initial FETCH/request mapping is not one-to-one")
    sent = {row["update_id"] for row in rows if row["state"] == "sent"}
    terminal = {row["update_id"] for row in rows if row["state"] in ("accepted", "failed")}
    unresolved = sent - terminal
    if unresolved:
        raise ValueError(f"unresolved REQUEST_UPDATE states: {sorted(unresolved)}")
    timestamps = [int(row["monotonic_us"]) for row in rows if row["monotonic_us"]]
    if timestamps != sorted(timestamps):
        raise ValueError("request update events are not monotonic")


def _group(rows: list[dict[str, str]], field: str) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        result[row[field]].append(row)
    return result


def iperf_mbps(path: Path) -> float:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("end", {}).get("sender_tcp_congestion") != "reno":
        raise ValueError(f"{path}: background is not Reno")
    received = data.get("end", {}).get("sum_received", {})
    if float(received.get("seconds", 0)) <= 0:
        raise ValueError(f"{path}: missing or prematurely terminated background flow")
    return float(received.get("bits_per_second", 0)) / 1_000_000


def case_summary(case: Path, manifest: dict[str, Any], epochs: int) -> dict[str, Any]:
    metadata = json.loads((case / "case-metadata.json").read_text(encoding="utf-8"))
    decisions = read_csv(case / "priority-decisions.csv")
    updates = read_csv(case / "request-updates.csv")
    deliveries = read_csv(case / "object-deliveries.csv")
    history = read_csv(case / "object-priority-history.csv")
    metrics = read_csv(case / "transport-metrics.csv")
    validate_decisions(manifest, decisions, epochs)
    expected_request_count = sum(int(refinement["object_count"]) > 0
                                 for tile in manifest["tiles"] for refinement in tile["refinements"])
    validate_updates(updates, expected_request_count)
    validate_object_coverage(manifest, deliveries)
    validate_history(deliveries, history)
    if metadata["mode"] == "prague" and metadata["ecn"] != "ect1":
        raise ValueError("Prague case lacks ECT(1) controller evidence")
    if metadata["mode"] == "reno" and metadata["ecn"] != "not-ect":
        raise ValueError("Reno case lacks Not-ECT controller evidence")
    if not metrics:
        raise ValueError("sender transport metrics are empty")

    decision_map = {(int(row["epoch"]), row["tile_id"], int(row["refinement"])): row
                    for row in decisions}
    delivered = [row for row in deliveries if row["status"] == "delivered"]
    delivered_bytes = sum(int(row["payload_bytes"]) for row in delivered)
    stale_bytes = 0
    breakdown: Counter[tuple[int, int, str]] = Counter()
    for row in delivered:
        epoch = int(row["subscriber_epoch"])
        decision = decision_map[(epoch, row["tile_id"], int(row["refinement"]))]
        if decision["viewport"] == "outside":
            stale_bytes += int(row["payload_bytes"])
        breakdown[(int(row["subscriber_priority"]), int(row["refinement"]),
                   decision["viewport"])] += int(row["payload_bytes"])

    sent = {row["update_id"]: row for row in updates if row["state"] == "sent"}
    accepted = [row for row in updates if row["state"] == "accepted"]
    send_accept_ms = [(int(row["monotonic_us"]) - int(sent[row["update_id"]]["monotonic_us"])) / 1000
                      for row in accepted if row["update_id"] in sent]
    by_request = _group(delivered, "request_id")
    accepted_next_ms = []
    for row in accepted:
        accepted_us = int(row["monotonic_us"])
        later = [int(obj["monotonic_us"]) for obj in by_request.get(row["request_id"], [])
                 if int(obj["monotonic_us"]) >= accepted_us]
        if later:
            accepted_next_ms.append((min(later) - accepted_us) / 1000)

    first, last = metrics[0], metrics[-1]
    elapsed = max(1, int(last["monotonic_us"]) - int(first["monotonic_us"]))
    goodput = max(0, int(last["data_sent_bytes"]) - int(first["data_sent_bytes"])) * 8 / elapsed
    final_epoch = epochs - 1
    delivered_keys = {key(row) for row in delivered}
    visible_levels = []
    for tile in manifest["tiles"]:
        visible = any(decision_map[(final_epoch, tile["id"], int(refinement["index"]))]["viewport"] != "outside"
                      for refinement in tile["refinements"] if int(refinement["object_count"]) > 0)
        if not visible:
            continue
        contiguous = 0
        for refinement in sorted(tile["refinements"], key=lambda item: int(item["index"])):
            if all((tile["id"], int(refinement["index"]), int(obj["id"])) in delivered_keys
                   for obj in refinement["objects"]):
                contiguous = int(refinement["index"]) + 1
            else:
                break
        visible_levels.append(contiguous)
    summary = {
        "case": case.name, "mode": metadata["mode"], "repetition": metadata["repetition"],
        "delivered_objects": len(delivered), "delivered_bytes": delivered_bytes,
        "outside_viewport_stale_fraction": stale_bytes / delivered_bytes if delivered_bytes else 0,
        "update_send_accept_ms": statistics.median(send_accept_ms) if send_accept_ms else None,
        "accepted_update_next_object_ms": statistics.median(accepted_next_ms) if accepted_next_ms else None,
        "foreground_goodput_mbps": goodput,
        "rtt_ms": statistics.median(int(row["rtt_us"]) for row in metrics) / 1000,
        "cwnd_bytes": statistics.median(int(row["cwnd_bytes"]) for row in metrics),
        "ce_packets": max(int(row["ce_packets"]) for row in metrics),
        "ect1_packets": max(int(row["ect1_packets"]) for row in metrics),
        "background_server_to_client_mbps": iperf_mbps(case / "iperf-server-to-client.json"),
        "background_client_to_server_mbps": iperf_mbps(case / "iperf-client-to-server.json"),
        "visible_contiguous_lod": statistics.mean(visible_levels) if visible_levels else 0,
        "breakdown": [{"priority": priority, "lod": lod, "viewport": viewport, "bytes": value}
                      for (priority, lod, viewport), value in sorted(breakdown.items())],
    }
    (case / "case-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def aggregate(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = ["outside_viewport_stale_fraction", "update_send_accept_ms",
               "accepted_update_next_object_ms", "foreground_goodput_mbps", "rtt_ms",
               "cwnd_bytes", "ce_packets", "background_server_to_client_mbps",
               "background_client_to_server_mbps", "visible_contiguous_lod"]
    result: dict[str, Any] = {"modes": {}}
    for mode in ("prague", "reno"):
        cases = [summary for summary in summaries if summary["mode"] == mode]
        if len(cases) != 3:
            raise ValueError(f"expected three {mode} repetitions, found {len(cases)}")
        result["modes"][mode] = {metric: median_band([float(case[metric]) for case in cases
                                                       if case[metric] is not None])
                                    for metric in metrics}
    prague = result["modes"]["prague"]
    reno = result["modes"]["reno"]
    latency_better = prague["accepted_update_next_object_ms"]["median"] < reno["accepted_update_next_object_ms"]["median"]
    stale_better = prague["outside_viewport_stale_fraction"]["median"] < reno["outside_viewport_stale_fraction"]["median"]
    result["classification"] = ("supports" if latency_better and stale_better else
                                "falsifies" if not latency_better and not stale_better else "inconclusive")
    return result


def write_plots(root: Path, summaries: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    first_case = root / summaries[0]["case"]
    decisions = read_csv(first_case / "priority-decisions.csv")
    epoch_rows = _group(decisions, "epoch")
    epochs = sorted(map(int, epoch_rows))
    yaw = [float(epoch_rows[str(epoch)][0]["yaw_degrees"]) for epoch in epochs]
    fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    axes[0].plot([epoch / 10 for epoch in epochs], yaw); axes[0].set_ylabel("Yaw (degrees)")
    for lod in range(4):
        axes[1].plot([epoch / 10 for epoch in epochs], [statistics.mean(int(row["priority"])
            for row in epoch_rows[str(epoch)] if int(row["refinement"]) == lod) for epoch in epochs], label=f"LoD {lod}")
    axes[1].set_ylabel("Mean priority"); axes[1].set_xlabel("Time (s)"); axes[1].legend()
    fig.tight_layout(); fig.savefig(root / "camera-priority-evolution.png", dpi=160); plt.close(fig)

    modes = ["prague", "reno"]
    def med(metric: str) -> list[float]:
        return [statistics.median(float(row[metric]) for row in summaries if row["mode"] == mode) for mode in modes]
    fig, ax = plt.subplots(figsize=(6, 4)); ax.bar(modes, med("delivered_bytes")); ax.set_ylabel("Delivered bytes")
    fig.tight_layout(); fig.savefig(root / "delivered-bytes.png", dpi=160); plt.close(fig)
    fig, ax = plt.subplots(figsize=(6, 4)); ax.bar(modes, med("accepted_update_next_object_ms")); ax.set_ylabel("Accepted update to next object (ms)")
    fig.tight_layout(); fig.savefig(root / "update-latency.png", dpi=160); plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(9, 7))
    for ax, metric, label in zip(axes.flat, ["foreground_goodput_mbps", "rtt_ms", "cwnd_bytes", "background_server_to_client_mbps"],
                                 ["Foreground Mbit/s", "RTT (ms)", "cwnd (bytes)", "Background Mbit/s"]):
        ax.bar(modes, med(metric)); ax.set_ylabel(label)
    fig.tight_layout(); fig.savefig(root / "transport-outcomes.png", dpi=160); plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(9, 4))
    axes[0].bar(modes, med("visible_contiguous_lod")); axes[0].set_ylabel("Visible contiguous LoD")
    axes[1].bar(modes, med("outside_viewport_stale_fraction")); axes[1].set_ylabel("Outside stale-byte fraction")
    fig.tight_layout(); fig.savefig(root / "visible-progress-stale-bytes.png", dpi=160); plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=501)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.preflight:
        summary = case_summary(args.run / "preflight", manifest, args.epochs)
        history = read_csv(args.run / "preflight/object-priority-history.csv")
        accepted_before_terminal = sum(1 for rows in _group(history, "object_id").values()
                                       if sum(row["kind"] == "accepted" for row in rows) >= 2)
        if accepted_before_terminal == 0:
            raise ValueError("preflight did not prove multiple accepted updates on an undelivered object")
        (args.run / "preflight-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return
    summaries = [case_summary(case, manifest, args.epochs) for case in sorted(args.run.glob("*-rep-*"))]
    result = aggregate(summaries)
    (args.run / "aggregate-summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    with (args.run / "case-summaries.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = [field for field in summaries[0] if field != "breakdown"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(
            {field: row[field] for field in fields} for row in summaries)
    if not args.validate_only:
        write_plots(args.run, summaries)


if __name__ == "__main__":
    main()
