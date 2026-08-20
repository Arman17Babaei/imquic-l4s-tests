#!/usr/bin/env python3
"""Analyze packet-order correction in the partial-L4S 3DGS experiment."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

RENO_PORT = 4443
PRAGUE_PORT = 4444


@dataclass(frozen=True)
class Packet:
    time_s: float
    source_port: int
    payload_bytes: int
    fingerprint: str
    occurrence: int = 0

    @property
    def key(self) -> tuple[str, int]:
        return self.fingerprint, self.occurrence


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _fingerprint(payload_hex: str, source_port: int, udp_length: int) -> str:
    normalized = payload_hex.replace(":", "").strip().lower()
    if not normalized:
        raise ValueError("capture row has no UDP payload")
    digest = hashlib.sha256()
    digest.update(str(source_port).encode("ascii"))
    digest.update(b"|")
    digest.update(str(udp_length).encode("ascii"))
    digest.update(b"|")
    digest.update(normalized.encode("ascii"))
    return digest.hexdigest()


def _assign_occurrences(rows: list[Packet]) -> list[Packet]:
    seen: dict[str, int] = {}
    result: list[Packet] = []
    for row in sorted(rows, key=lambda packet: packet.time_s):
        occurrence = seen.get(row.fingerprint, 0)
        seen[row.fingerprint] = occurrence + 1
        result.append(
            Packet(
                time_s=row.time_s,
                source_port=row.source_port,
                payload_bytes=row.payload_bytes,
                fingerprint=row.fingerprint,
                occurrence=occurrence,
            )
        )
    return result


def parse_tshark_rows(rows: list[dict[str, str]]) -> list[Packet]:
    packets: list[Packet] = []
    for row in rows:
        source_port = int(row["udp.srcport"])
        if source_port not in (RENO_PORT, PRAGUE_PORT):
            continue
        udp_length = int(row["udp.length"])
        payload_bytes = max(0, udp_length - 8)
        fingerprint = _fingerprint(
            row["udp.payload"], source_port=source_port, udp_length=udp_length
        )
        packets.append(
            Packet(
                time_s=float(row["frame.time_epoch"]),
                source_port=source_port,
                payload_bytes=payload_bytes,
                fingerprint=fingerprint,
            )
        )
    return _assign_occurrences(packets)


def read_capture(path: Path, *, server_ip: str = "10.0.0.1") -> list[Packet]:
    if shutil.which("tshark") is None:
        raise RuntimeError("tshark is required to analyze packet captures")
    command = [
        "tshark", "-r", str(path),
        "-Y",
        (
            f"ip.src == {server_ip} && udp && "
            f"(udp.srcport == {RENO_PORT} || udp.srcport == {PRAGUE_PORT})"
        ),
        "-T", "fields",
        "-E", "header=y",
        "-E", "separator=,",
        "-E", "quote=d",
        "-E", "occurrence=f",
        "-e", "frame.time_epoch",
        "-e", "udp.srcport",
        "-e", "udp.length",
        "-e", "udp.payload",
    ]
    output = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    reader = csv.DictReader(output.splitlines())
    return parse_tshark_rows(list(reader))


def _packet_map(packets: list[Packet]) -> dict[tuple[str, int], Packet]:
    mapped: dict[tuple[str, int], Packet] = {}
    for packet in packets:
        if packet.key in mapped:
            raise ValueError(f"duplicate packet key after occurrence assignment: {packet.key}")
        mapped[packet.key] = packet
    return mapped


class _Fenwick:
    def __init__(self, size: int) -> None:
        self.values = [0] * (size + 1)

    def add(self, index: int, value: int) -> None:
        index += 1
        while index < len(self.values):
            self.values[index] += value
            index += index & -index

    def prefix_sum(self, end: int) -> int:
        """Return sum over [0, end)."""
        total = 0
        index = end
        while index > 0:
            total += self.values[index]
            index -= index & -index
        return total

    def total(self) -> int:
        return self.prefix_sum(len(self.values) - 1)


def analyze_packet_order(
    ingress: list[Packet],
    provider_egress: list[Packet],
    downstream_egress: list[Packet],
) -> dict[str, object]:
    """Find Reno->Prague order inversions created inside the provider switch.

    An inversion is a pair (Reno R, Prague P) where R reaches provider ingress
    before P, but P leaves provider egress before R. The implementation is
    O(n log n): a Fenwick tree counts per-Prague overtaken Reno traffic, while
    a reverse scan assigns one concrete Prague witness to every Reno packet
    that was overtaken at least once.
    """
    import bisect

    ingress_map = _packet_map(ingress)
    provider_map = _packet_map(provider_egress)
    downstream_map = _packet_map(downstream_egress)

    common_provider = set(ingress_map) & set(provider_map)
    common_all = common_provider & set(downstream_map)
    ordered = sorted(common_provider, key=lambda key: ingress_map[key].time_s)

    reno_keys = [
        key for key in ordered if ingress_map[key].source_port == RENO_PORT
    ]
    prague_keys = [
        key for key in ordered if ingress_map[key].source_port == PRAGUE_PORT
    ]

    reno_egress_times = sorted(provider_map[key].time_s for key in reno_keys)
    count_tree = _Fenwick(len(reno_egress_times))
    byte_tree = _Fenwick(len(reno_egress_times))
    per_prague_counts: list[int] = []
    per_prague_bytes: list[int] = []
    inversion_pairs = 0
    for key in ordered:
        packet = ingress_map[key]
        if packet.source_port == RENO_PORT:
            rank = bisect.bisect_left(reno_egress_times, provider_map[key].time_s)
            count_tree.add(rank, 1)
            byte_tree.add(rank, packet.payload_bytes)
            continue
        if packet.source_port != PRAGUE_PORT:
            continue
        split = bisect.bisect_right(
            reno_egress_times, provider_map[key].time_s
        )
        overtaken_count = count_tree.total() - count_tree.prefix_sum(split)
        overtaken_bytes = byte_tree.total() - byte_tree.prefix_sum(split)
        inversion_pairs += overtaken_count
        if overtaken_count:
            per_prague_counts.append(overtaken_count)
            per_prague_bytes.append(overtaken_bytes)

    witness_by_reno: dict[tuple[str, int], tuple[str, int]] = {}
    min_prague_key: tuple[str, int] | None = None
    min_prague_egress = math.inf
    for key in reversed(ordered):
        packet = ingress_map[key]
        if packet.source_port == PRAGUE_PORT:
            egress = provider_map[key].time_s
            if egress < min_prague_egress:
                min_prague_egress = egress
                min_prague_key = key
        elif (
            packet.source_port == RENO_PORT
            and min_prague_key is not None
            and min_prague_egress < provider_map[key].time_s
        ):
            witness_by_reno[key] = min_prague_key

    unique_overtaken_bytes = sum(
        ingress_map[key].payload_bytes for key in witness_by_reno
    )
    reno_payload_bytes = sum(
        ingress_map[key].payload_bytes for key in reno_keys
    )

    preserved = 0
    witness_pairs_observed_downstream = 0
    provider_gaps_ms: list[float] = []
    downstream_gaps_ms: list[float] = []
    amplification: list[float] = []
    for r_key, p_key in witness_by_reno.items():
        if r_key not in downstream_map or p_key not in downstream_map:
            continue
        witness_pairs_observed_downstream += 1
        provider_gap = (
            provider_map[r_key].time_s - provider_map[p_key].time_s
        ) * 1000.0
        downstream_gap = (
            downstream_map[r_key].time_s - downstream_map[p_key].time_s
        ) * 1000.0
        provider_gaps_ms.append(provider_gap)
        downstream_gaps_ms.append(downstream_gap)
        if downstream_gap > 0:
            preserved += 1
        if provider_gap > 0.001 and downstream_gap > 0:
            amplification.append(downstream_gap / provider_gap)

    return {
        "capture_counts": {
            "provider_ingress_packets": len(ingress),
            "provider_egress_packets": len(provider_egress),
            "downstream_egress_packets": len(downstream_egress),
            "provider_matched_packets": len(common_provider),
            "all_three_matched_packets": len(common_all),
            "reno_provider_matched_packets": len(reno_keys),
            "prague_provider_matched_packets": len(prague_keys),
            "provider_match_fraction": (
                len(common_provider) / len(ingress) if ingress else 0.0
            ),
            "all_three_match_fraction": (
                len(common_all) / len(common_provider) if common_provider else 0.0
            ),
        },
        "provider_reordering": {
            "inversion_pairs": inversion_pairs,
            "prague_packets_with_overtake": len(per_prague_bytes),
            "fraction_prague_packets_with_overtake": (
                len(per_prague_bytes) / len(prague_keys) if prague_keys else 0.0
            ),
            "unique_reno_packets_overtaken": len(witness_by_reno),
            "unique_reno_payload_bytes_overtaken": unique_overtaken_bytes,
            "fraction_reno_payload_bytes_overtaken": (
                unique_overtaken_bytes / reno_payload_bytes
                if reno_payload_bytes else 0.0
            ),
            "overtaken_packets_per_prague_packet": {
                "mean": statistics.fmean(per_prague_counts)
                if per_prague_counts else 0.0,
                "p50": _percentile([float(v) for v in per_prague_counts], 0.50),
                "p95": _percentile([float(v) for v in per_prague_counts], 0.95),
                "max": max(per_prague_counts, default=0),
            },
            "overtaken_payload_bytes_per_prague_packet": {
                "mean": statistics.fmean(per_prague_bytes)
                if per_prague_bytes else 0.0,
                "p50": _percentile([float(v) for v in per_prague_bytes], 0.50),
                "p95": _percentile([float(v) for v in per_prague_bytes], 0.95),
                "max": max(per_prague_bytes, default=0),
            },
        },
        "downstream_persistence": {
            "unique_overtaken_reno_witness_pairs_observed_downstream":
                witness_pairs_observed_downstream,
            "witness_pairs_preserved": preserved,
            "preservation_fraction": (
                preserved / witness_pairs_observed_downstream
                if witness_pairs_observed_downstream else 0.0
            ),
            "provider_witness_gap_ms": {
                "p50": _percentile(provider_gaps_ms, 0.50),
                "p95": _percentile(provider_gaps_ms, 0.95),
                "max": max(provider_gaps_ms, default=None),
            },
            "downstream_witness_gap_ms": {
                "p50": _percentile(downstream_gaps_ms, 0.50),
                "p95": _percentile(downstream_gaps_ms, 0.95),
                "max": max(downstream_gaps_ms, default=None),
            },
            "gap_amplification_ratio": {
                "p50": _percentile(amplification, 0.50),
                "p95": _percentile(amplification, 0.95),
                "max": max(amplification, default=None),
            },
            "interpretation": (
                "Each overtaken Reno packet is paired with one later-ingress "
                "Prague packet that demonstrably exited the provider first. "
                "Preservation tests that concrete inversion at the Classic egress."
            ),
        },
    }


def analyze_case(case: Path, *, server_ip: str) -> dict[str, object]:
    paths = {
        "l4s_ingress": case / "l4s_ingress.pcap",
        "l4s_egress": case / "l4s_egress.pcap",
        "classic_egress": case / "classic_egress.pcap",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing packet captures: {', '.join(missing)}")
    result = analyze_packet_order(
        read_capture(paths["l4s_ingress"], server_ip=server_ip),
        read_capture(paths["l4s_egress"], server_ip=server_ip),
        read_capture(paths["classic_egress"], server_ip=server_ip),
    )
    result["case"] = case.name
    destination = case / "reordering-analysis.json"
    destination.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--server-ip", default="10.0.0.1")
    args = parser.parse_args()

    cases = sorted(
        path for path in args.root.iterdir()
        if path.is_dir() and path.name.startswith("l4s-")
    )
    if not cases:
        raise SystemExit(f"no l4s-* case directories under {args.root}")
    results = [analyze_case(case, server_ip=args.server_ip) for case in cases]
    summary = {
        "scenario": "3dgs-partial-l4s-post-send-reordering",
        "cases": results,
    }
    (args.root / "reordering-analysis.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
