#!/usr/bin/env python3
"""Direct queue-residence analysis and support gates for thesis Figures 6--8."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict, deque
import json
import math
import subprocess
from pathlib import Path
from typing import Iterable


PERCENTILES = (50.0, 95.0, 99.0, 99.9)


def percentile(values: list[float], pct: float) -> float:
    """Return a linearly interpolated percentile without optional dependencies."""
    if not values:
        raise ValueError("percentile of empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _packet_rows(path: Path, server_ip: str, start: float, finish: float):
    fields = (
        "frame.time_epoch", "ip.src", "ip.dst", "ip.id", "ip.proto",
        "udp.srcport", "udp.dstport", "udp.length", "tcp.srcport",
        "tcp.dstport", "tcp.seq_raw", "tcp.ack_raw", "tcp.len", "tcp.flags",
    )
    command = ["tshark", "-n", "-r", str(path), "-Y", (
        f"ip.src == {server_ip} && frame.time_epoch >= {start:.6f} && "
        f"frame.time_epoch <= {finish:.6f} && udp.port == 4443"
    ), "-T", "fields", "-E", "separator=\t", "-E", "occurrence=f"]
    for field in fields:
        command.extend(("-e", field))
    output = subprocess.run(command, check=True, text=True,
                            stdout=subprocess.PIPE).stdout
    for line in output.splitlines():
        columns = line.split("\t")
        columns += [""] * (len(fields) - len(columns))
        timestamp = float(columns[0])
        proto = columns[4]
        if proto == "17":
            transport = ("udp", columns[5], columns[6], columns[7])
        elif proto == "6":
            transport = (
                "tcp", columns[8], columns[9], columns[10], columns[11],
                columns[12], columns[13],
            )
        else:
            continue
        # IP ID and transport sequence/length fields are immutable across the
        # switch. ECN and checksums are intentionally excluded.
        identity = (columns[1], columns[2], columns[3], *transport)
        yield timestamp, identity


def direct_queue_delays(case: Path) -> dict:
    metadata = json.loads((case / "metadata.json").read_text())
    start = float(metadata.get("measurement_started_epoch",
                               metadata["foreground_started_epoch"]))
    finish = float(metadata.get("measurement_finished_epoch",
                                metadata["foreground_finished_epoch"]))
    server_ip = metadata["server_ip"]
    ingress_rows = list(_packet_rows(case / "switch-server.pcap", server_ip,
                                     start, finish))
    egress_rows = list(_packet_rows(case / "switch-client.pcap", server_ip,
                                    start, finish))
    ingress = defaultdict(deque)
    for timestamp, identity in ingress_rows:
        ingress[identity].append(timestamp)
    matched = []
    for timestamp, identity in egress_rows:
        candidates = ingress.get(identity)
        if not candidates:
            continue
        arrived = candidates.popleft()
        residence = (timestamp - arrived) * 1000.0
        if residence >= -0.05:
            matched.append((timestamp - start, max(0.0, residence)))
    denominator = max(len(ingress_rows), len(egress_rows), 1)
    coverage = len(matched) / denominator
    if not matched:
        raise RuntimeError(f"{case.name}: no packets matched across bottleneck")
    sojourn_ms = [value for _, value in matched]
    baseline = min(sojourn_ms)
    queue_ms = [max(0.0, value - baseline) for value in sojourn_ms]
    result = {
        "definition": (
            "foreground UDP/4443 same-clock bottleneck residence minus case "
            "minimum residence"
        ),
        "ingress_packets": len(ingress_rows),
        "egress_packets": len(egress_rows),
        "matched_packets": len(queue_ms),
        "match_coverage": coverage,
        "minimum_residence_ms": baseline,
        "queue_delay_ms": queue_ms,
        "percentiles_ms": {
            str(pct): percentile(queue_ms, pct) for pct in PERCENTILES
        },
    }
    (case / "queue-delay.json").write_text(json.dumps(result, indent=2) + "\n")
    with (case / "queue-delay.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("measurement_time_s", "queue_delay_ms"))
        writer.writerows(
            (time_s, value) for (time_s, _), value in zip(matched, queue_ms)
        )
    return result


def _iperf_overlap_mbps(path: Path, warmup: float, duration: float) -> float:
    data = json.loads(path.read_text())
    bits = 0.0
    for interval in data.get("intervals", []):
        summary = interval.get("sum") or interval.get("sum_received") or {}
        start = float(summary.get("start", 0.0))
        end = float(summary.get("end", start))
        overlap = max(0.0, min(end, warmup + duration) - max(start, warmup))
        interval_duration = max(end - start, 1e-12)
        bits += float(summary.get("bytes", 0)) * 8.0 * overlap / interval_duration
    return bits / duration / 1_000_000.0


def _forward_wire_mbps(case: Path, server_ip: str, start: float,
                       finish: float) -> float:
    command = [
        "tshark", "-n", "-r", str(case / "switch-client.pcap"), "-Y",
        (f"ip.src == {server_ip} && frame.time_epoch >= {start:.6f} && "
         f"frame.time_epoch <= {finish:.6f}"),
        "-T", "fields", "-e", "frame.len",
    ]
    output = subprocess.run(command, check=True, text=True,
                            stdout=subprocess.PIPE).stdout
    octets = sum(int(value) for value in output.splitlines() if value)
    duration = finish - start
    return octets * 8.0 / duration / 1_000_000.0


def case_summary(case: Path) -> dict:
    metadata = json.loads((case / "metadata.json").read_text())
    queue = direct_queue_delays(case)
    duration = float(metadata["duration_seconds"])
    received = json.loads((case / "subscriber-result.json").read_text())
    foreground_mbps = float(received["received_bytes"]) * 8.0 / duration / 1_000_000.0
    cubic = []
    for path in sorted(case.glob("iperf-client-*.json")):
        cubic.append(_iperf_overlap_mbps(path, float(metadata["warmup_seconds"]),
                                         duration))
    bottleneck = float(metadata["bottleneck_mbps"])
    application_total = foreground_mbps + sum(cubic)
    wire_mbps = _forward_wire_mbps(
        case, metadata["server_ip"],
        float(metadata.get("measurement_started_epoch",
                           metadata["foreground_started_epoch"])),
        float(metadata.get("measurement_finished_epoch",
                           metadata["foreground_finished_epoch"])),
    )
    throughputs = [foreground_mbps, *cubic]
    fairness = ((sum(throughputs) ** 2 / (len(throughputs) *
                 sum(value * value for value in throughputs)))
                if throughputs and any(throughputs) else 0.0)
    summary = {
        "case": case.name,
        "mode": metadata["mode"],
        "repetition": metadata["repetition"],
        "background_flow_count": metadata["background_flow_count"],
        "foreground_mbps": foreground_mbps,
        "cubic_flow_mbps": cubic,
        "cubic_aggregate_mbps": sum(cubic),
        "application_goodput_mbps": application_total,
        "forward_wire_mbps": wire_mbps,
        "utilization_percent": wire_mbps / bottleneck * 100.0,
        "jain_fairness": fairness,
        "queue": {key: value for key, value in queue.items()
                  if key != "queue_delay_ms"},
    }
    (case / "case-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def evaluate_pair(classic_dir: Path, l4s_dir: Path) -> dict:
    classic = case_summary(classic_dir)
    l4s = case_summary(l4s_dir)
    if classic["background_flow_count"] != l4s["background_flow_count"]:
        raise ValueError("matched pair has different Cubic flow counts")
    count = int(l4s["background_flow_count"])
    checks = {
        "packet_match_coverage_classic_ge_0.95": classic["queue"]["match_coverage"] >= 0.95,
        "packet_match_coverage_l4s_ge_0.95": l4s["queue"]["match_coverage"] >= 0.95,
        "l4s_p95_lower": l4s["queue"]["percentiles_ms"]["95.0"] < classic["queue"]["percentiles_ms"]["95.0"],
        "l4s_p99_lower": l4s["queue"]["percentiles_ms"]["99.0"] < classic["queue"]["percentiles_ms"]["99.0"],
        "l4s_utilization_ge_90": l4s["utilization_percent"] >= 90.0,
        "l4s_utilization_within_5pp": l4s["utilization_percent"] >= classic["utilization_percent"] - 5.0,
    }
    if count == 1:
        checks["l4s_foreground_share_ge_10"] = l4s["foreground_mbps"] >= 2.0
        checks["cubic_share_ge_10"] = l4s["cubic_aggregate_mbps"] >= 2.0
    elif count in (2, 4):
        checks["every_cubic_flow_active"] = (
            len(l4s["cubic_flow_mbps"]) == count and
            all(value > 0.01 for value in l4s["cubic_flow_mbps"])
        )
        checks["cubic_aggregate_share_ge_10"] = l4s["cubic_aggregate_mbps"] >= 2.0
    method_keys = {key for key in checks if key.startswith("packet_match")}
    method_valid = all(checks[key] for key in method_keys)
    support = all(value for key, value in checks.items() if key not in method_keys)
    result = {
        "classic": classic,
        "l4s": l4s,
        "checks": checks,
        "method_valid": method_valid,
        "supports_l4s_claim": support if method_valid else None,
    }
    output = l4s_dir.parent / (
        f"pair-flow-{count:02d}-rep-{int(l4s['repetition']):02d}-support.json"
    )
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("classic", type=Path, nargs="?")
    parser.add_argument("l4s", type=Path, nargs="?")
    parser.add_argument("--gate", action="store_true",
                        help="exit 2 for method failure and 3 for valid negative result")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert percentile([0.0, 10.0], 50.0) == 5.0
        assert percentile([4.0], 99.9) == 4.0
        print("thesis network matrix analyzer self-test passed")
        return
    if args.classic is None or args.l4s is None:
        parser.error("classic and l4s case directories are required")
    result = evaluate_pair(args.classic, args.l4s)
    print(json.dumps(result["checks"], indent=2))
    if args.gate:
        if not result["method_valid"]:
            raise SystemExit(2)
        if not result["supports_l4s_claim"]:
            raise SystemExit(3)


if __name__ == "__main__":
    main()
