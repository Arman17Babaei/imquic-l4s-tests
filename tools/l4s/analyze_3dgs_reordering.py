#!/usr/bin/env python3
"""Analyze Base/Prague overtaking of Classic Enhancement/Reno packets."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import ipaddress
import json
import math
import statistics
import struct
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


def _summary(values: list[float]) -> dict[str, float | None]:
    return {
        "mean": statistics.fmean(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


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


def _fingerprint_bytes(payload: bytes, source_port: int, udp_length: int) -> str:
    digest = hashlib.sha256()
    digest.update(str(source_port).encode("ascii"))
    digest.update(b"|")
    digest.update(str(udp_length).encode("ascii"))
    digest.update(b"|")
    digest.update(payload)
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
        packets.append(
            Packet(
                time_s=float(row["frame.time_epoch"]),
                source_port=source_port,
                payload_bytes=max(0, udp_length - 8),
                fingerprint=_fingerprint(row["udp.payload"], source_port, udp_length),
            )
        )
    return _assign_occurrences(packets)


def read_capture(path: Path, *, server_ip: str = "10.0.0.1") -> list[Packet]:
    source_address = ipaddress.IPv4Address(server_ip).packed
    packets: list[Packet] = []
    with path.open("rb") as stream:
        magic = stream.read(4)
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1_000_000.0),
            b"\xa1\xb2\xc3\xd4": (">", 1_000_000.0),
            b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000.0),
            b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000.0),
        }
        if magic not in formats:
            raise ValueError(f"{path}: unsupported pcap magic {magic.hex()}")
        endian, timestamp_scale = formats[magic]
        global_rest = stream.read(20)
        if len(global_rest) != 20:
            raise ValueError(f"{path}: truncated pcap header")
        link_type = struct.unpack(f"{endian}I", global_rest[16:20])[0]
        if link_type != 1:
            raise ValueError(f"{path}: expected Ethernet link type, got {link_type}")
        while True:
            packet_header = stream.read(16)
            if not packet_header:
                break
            if len(packet_header) != 16:
                raise ValueError(f"{path}: truncated packet header")
            seconds, fraction, captured_length, _ = struct.unpack(
                f"{endian}IIII", packet_header
            )
            frame = stream.read(captured_length)
            if len(frame) != captured_length:
                raise ValueError(f"{path}: truncated packet data")
            if len(frame) < 14:
                continue
            offset = 14
            ether_type = struct.unpack("!H", frame[12:14])[0]
            while ether_type in (0x8100, 0x88A8):
                if len(frame) < offset + 4:
                    break
                ether_type = struct.unpack("!H", frame[offset + 2:offset + 4])[0]
                offset += 4
            if ether_type != 0x0800 or len(frame) < offset + 20:
                continue
            version_ihl = frame[offset]
            if version_ihl >> 4 != 4:
                continue
            ip_header_length = (version_ihl & 0x0F) * 4
            if ip_header_length < 20 or len(frame) < offset + ip_header_length:
                continue
            if frame[offset + 9] != 17:
                continue
            if frame[offset + 12:offset + 16] != source_address:
                continue
            fragment = struct.unpack("!H", frame[offset + 6:offset + 8])[0]
            if fragment & 0x1FFF:
                continue
            udp_offset = offset + ip_header_length
            if len(frame) < udp_offset + 8:
                continue
            source_port, _, udp_length, _ = struct.unpack(
                "!HHHH", frame[udp_offset:udp_offset + 8]
            )
            if source_port not in (RENO_PORT, PRAGUE_PORT) or udp_length < 8:
                continue
            udp_end = udp_offset + udp_length
            if udp_end > len(frame):
                continue
            payload = frame[udp_offset + 8:udp_end]
            packets.append(
                Packet(
                    time_s=seconds + fraction / timestamp_scale,
                    source_port=source_port,
                    payload_bytes=len(payload),
                    fingerprint=_fingerprint_bytes(payload, source_port, udp_length),
                )
            )
    return _assign_occurrences(packets)


def read_packet_log(path: Path) -> list[Packet]:
    packets: list[Packet] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            packets.append(
                Packet(
                    time_s=float(row["time_s"]),
                    source_port=int(row["source_port"]),
                    payload_bytes=int(row["payload_bytes"]),
                    fingerprint=row["fingerprint"],
                    occurrence=int(row["occurrence"]),
                )
            )
    return sorted(packets, key=lambda packet: packet.time_s)


def _packet_map(packets: list[Packet]) -> dict[tuple[str, int], Packet]:
    result: dict[tuple[str, int], Packet] = {}
    for packet in packets:
        if packet.key in result:
            raise ValueError(f"duplicate packet key: {packet.key}")
        result[packet.key] = packet
    return result


class _Fenwick:
    def __init__(self, size: int) -> None:
        self.values = [0] * (size + 1)

    def add(self, index: int, value: int) -> None:
        index += 1
        while index < len(self.values):
            self.values[index] += value
            index += index & -index

    def prefix_sum(self, end: int) -> int:
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
    """Measure queue overlap, overtaking, and downstream preservation.

    An *opportunity* exists for pair (R, B) when Classic Enhancement packet R
    entered the provider before Base/Prague packet B and R was still resident
    at the provider when B arrived: R_in < B_in < R_out.

    An *inversion* additionally requires B_out < R_out. Every inversion is
    therefore a successful use of a measured provider overlap opportunity.
    """
    ingress_map = _packet_map(ingress)
    provider_map = _packet_map(provider_egress)
    downstream_map = _packet_map(downstream_egress)
    common_provider = set(ingress_map) & set(provider_map)
    common_all = common_provider & set(downstream_map)
    ordered = sorted(common_provider, key=lambda key: ingress_map[key].time_s)
    reno_keys = [key for key in ordered if ingress_map[key].source_port == RENO_PORT]
    prague_keys = [key for key in ordered if ingress_map[key].source_port == PRAGUE_PORT]

    reno_egress_times = sorted(provider_map[key].time_s for key in reno_keys)
    count_tree = _Fenwick(len(reno_egress_times))
    byte_tree = _Fenwick(len(reno_egress_times))
    opportunity_pairs = 0
    opportunity_byte_pairs = 0
    per_prague_opportunity_counts: list[int] = []
    per_prague_opportunity_bytes: list[int] = []
    inversion_pairs = 0
    per_prague_inversion_counts: list[int] = []
    per_prague_inversion_bytes: list[int] = []

    for key in ordered:
        packet = ingress_map[key]
        if packet.source_port == RENO_PORT:
            rank = bisect.bisect_left(reno_egress_times, provider_map[key].time_s)
            count_tree.add(rank, 1)
            byte_tree.add(rank, packet.payload_bytes)
            continue

        # All Reno packets currently in the tree entered before this Base packet.
        # Those whose provider egress is later than Base ingress are still resident.
        opportunity_split = bisect.bisect_right(
            reno_egress_times, ingress_map[key].time_s
        )
        opportunity_count = count_tree.total() - count_tree.prefix_sum(
            opportunity_split
        )
        opportunity_bytes = byte_tree.total() - byte_tree.prefix_sum(
            opportunity_split
        )
        opportunity_pairs += opportunity_count
        opportunity_byte_pairs += opportunity_bytes
        if opportunity_count:
            per_prague_opportunity_counts.append(opportunity_count)
            per_prague_opportunity_bytes.append(opportunity_bytes)

        # Successful inversion: Base leaves before the resident Reno packet.
        inversion_split = bisect.bisect_right(
            reno_egress_times, provider_map[key].time_s
        )
        inversion_count = count_tree.total() - count_tree.prefix_sum(
            inversion_split
        )
        inversion_bytes = byte_tree.total() - byte_tree.prefix_sum(
            inversion_split
        )
        inversion_pairs += inversion_count
        if inversion_count:
            per_prague_inversion_counts.append(inversion_count)
            per_prague_inversion_bytes.append(inversion_bytes)

    prague_ingress_times = sorted(ingress_map[key].time_s for key in prague_keys)
    unique_opportunity_reno = []
    for key in reno_keys:
        r_in = ingress_map[key].time_s
        r_out = provider_map[key].time_s
        candidate = bisect.bisect_right(prague_ingress_times, r_in)
        if candidate < len(prague_ingress_times) and prague_ingress_times[candidate] < r_out:
            unique_opportunity_reno.append(key)
    unique_opportunity_bytes = sum(
        ingress_map[key].payload_bytes for key in unique_opportunity_reno
    )

    # Select one concrete later-ingress Base witness for each Reno packet that
    # was actually overtaken, for downstream persistence measurements.
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
    reno_payload_bytes = sum(ingress_map[key].payload_bytes for key in reno_keys)

    preserved = 0
    downstream_observed = 0
    provider_gaps_ms: list[float] = []
    downstream_gaps_ms: list[float] = []
    amplification: list[float] = []
    for r_key, p_key in witness_by_reno.items():
        if r_key not in downstream_map or p_key not in downstream_map:
            continue
        downstream_observed += 1
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

    reno_sojourn = [
        (provider_map[key].time_s - ingress_map[key].time_s) * 1000.0
        for key in reno_keys
    ]
    prague_sojourn = [
        (provider_map[key].time_s - ingress_map[key].time_s) * 1000.0
        for key in prague_keys
    ]

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
        "provider_sojourn_ms": {
            "classic_enhancement_reno": _summary(reno_sojourn),
            "base_prague": _summary(prague_sojourn),
        },
        "provider_opportunity": {
            "opportunity_pairs": opportunity_pairs,
            "opportunity_payload_byte_pairs": opportunity_byte_pairs,
            "base_packets_with_opportunity": len(per_prague_opportunity_counts),
            "fraction_base_packets_with_opportunity": (
                len(per_prague_opportunity_counts) / len(prague_keys)
                if prague_keys else 0.0
            ),
            "unique_classic_enhancement_packets_with_opportunity": len(
                unique_opportunity_reno
            ),
            "unique_classic_enhancement_payload_bytes_with_opportunity": (
                unique_opportunity_bytes
            ),
            "resident_classic_packets_per_base_packet": _summary(
                [float(value) for value in per_prague_opportunity_counts]
            ),
            "resident_classic_payload_bytes_per_base_packet": _summary(
                [float(value) for value in per_prague_opportunity_bytes]
            ),
        },
        "provider_reordering": {
            "inversion_pairs": inversion_pairs,
            "prague_packets_with_overtake": len(per_prague_inversion_bytes),
            "fraction_prague_packets_with_overtake": (
                len(per_prague_inversion_bytes) / len(prague_keys)
                if prague_keys else 0.0
            ),
            "unique_reno_packets_overtaken": len(witness_by_reno),
            "unique_reno_payload_bytes_overtaken": unique_overtaken_bytes,
            "fraction_reno_payload_bytes_overtaken": (
                unique_overtaken_bytes / reno_payload_bytes
                if reno_payload_bytes else 0.0
            ),
            "inversion_success_fraction_of_opportunities": (
                inversion_pairs / opportunity_pairs if opportunity_pairs else None
            ),
            "unique_overtaken_fraction_of_opportunity_bytes": (
                unique_overtaken_bytes / unique_opportunity_bytes
                if unique_opportunity_bytes else None
            ),
            "overtaken_packets_per_prague_packet": _summary(
                [float(value) for value in per_prague_inversion_counts]
            ),
            "overtaken_payload_bytes_per_prague_packet": _summary(
                [float(value) for value in per_prague_inversion_bytes]
            ),
        },
        "downstream_persistence": {
            "unique_overtaken_reno_witness_pairs_observed_downstream": downstream_observed,
            "witness_pairs_preserved": preserved,
            "preservation_fraction": (
                preserved / downstream_observed if downstream_observed else 0.0
            ),
            "provider_witness_gap_ms": _summary(provider_gaps_ms),
            "downstream_witness_gap_ms": _summary(downstream_gaps_ms),
            "gap_amplification_ratio": _summary(amplification),
        },
    }


def evaluate_acceptance(
    result: dict[str, object],
    *,
    requested_l4s_fraction: float,
    capture_stats: dict[str, dict[str, object]],
    minimum_provider_match_fraction: float,
    minimum_downstream_match_fraction: float,
) -> dict[str, object]:
    """Validate measurement quality without predetermining hypothesis outcome."""
    counts = result["capture_counts"]
    opportunity = result["provider_opportunity"]
    reorder = result["provider_reordering"]
    persistence = result["downstream_persistence"]
    cross_class_applicable = 0.0 < requested_l4s_fraction < 1.0
    failures: list[str] = []
    if requested_l4s_fraction < 1.0 and counts["reno_provider_matched_packets"] == 0:
        failures.append("no matched Classic Enhancement/Reno packets")
    if requested_l4s_fraction > 0.0 and counts["prague_provider_matched_packets"] == 0:
        failures.append("no matched Base/Prague packets")
    if counts["provider_match_fraction"] < minimum_provider_match_fraction:
        failures.append("provider packet match fraction below threshold")
    if counts["all_three_match_fraction"] < minimum_downstream_match_fraction:
        failures.append("downstream packet match fraction below threshold")
    for label, stats in capture_stats.items():
        if int(stats.get("socket_drops") or 0) != 0:
            failures.append(f"compact capture socket drops observed at {label}")

    opportunity_observed = bool(
        cross_class_applicable and opportunity["opportunity_pairs"] > 0
    )
    mechanism_observed = bool(
        opportunity_observed and reorder["inversion_pairs"] > 0
    )
    downstream_witness_observed = bool(
        mechanism_observed
        and persistence["unique_overtaken_reno_witness_pairs_observed_downstream"] > 0
    )
    if not cross_class_applicable:
        outcome = "cross-class reordering not applicable"
    elif not opportunity_observed:
        outcome = "no provider overlap opportunity observed"
    elif mechanism_observed:
        outcome = "provider reordering observed"
    else:
        outcome = "provider overlap existed but no reordering observed"
    return {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "requested_l4s_byte_fraction": requested_l4s_fraction,
        "cross_class_reordering_applicable": cross_class_applicable,
        "opportunity_observed": opportunity_observed,
        "mechanism_observed": mechanism_observed,
        "downstream_witness_observed": downstream_witness_observed,
        "hypothesis_outcome": outcome,
        "minimum_provider_match_fraction": minimum_provider_match_fraction,
        "minimum_downstream_match_fraction": minimum_downstream_match_fraction,
        "interpretation": (
            "status validates measurement quality only; opportunity and mechanism "
            "fields describe the experimental outcome"
        ),
    }


def analyze_case(
    case: Path,
    *,
    server_ip: str,
    minimum_provider_match_fraction: float = 0.95,
    minimum_downstream_match_fraction: float = 0.95,
) -> dict[str, object]:
    compact_paths = {
        "provider_ingress": case / "provider_ingress.packet-order.csv",
        "provider_egress": case / "provider_egress.packet-order.csv",
        "downstream_egress": case / "downstream_egress.packet-order.csv",
    }
    pcap_paths = {
        "provider_ingress": case / "provider_ingress.pcap",
        "provider_egress": case / "provider_egress.pcap",
        "downstream_egress": case / "downstream_egress.pcap",
    }
    legacy_paths = {
        "provider_ingress": case / "l4s_ingress.pcap",
        "provider_egress": case / "l4s_egress.pcap",
        "downstream_egress": case / "classic_egress.pcap",
    }
    if all(path.is_file() for path in compact_paths.values()):
        paths = compact_paths
        reader = read_packet_log
        capture_format = "packet-order-csv-v1"
    elif all(path.is_file() for path in pcap_paths.values()):
        paths = pcap_paths
        reader = lambda path: read_capture(path, server_ip=server_ip)
        capture_format = "pcap"
    elif all(path.is_file() for path in legacy_paths.values()):
        paths = legacy_paths
        reader = lambda path: read_capture(path, server_ip=server_ip)
        capture_format = "pcap"
    else:
        raise FileNotFoundError(f"{case}: missing complete packet-order evidence")

    result = analyze_packet_order(
        reader(paths["provider_ingress"]),
        reader(paths["provider_egress"]),
        reader(paths["downstream_egress"]),
    )
    result["capture_format"] = capture_format
    capture_stats: dict[str, dict[str, object]] = {}
    if capture_format == "packet-order-csv-v1":
        for label, path in paths.items():
            stats_path = path.with_suffix(".stats.json")
            if not stats_path.is_file():
                raise FileNotFoundError(f"missing compact capture stats: {stats_path}")
            capture_stats[label] = json.loads(stats_path.read_text(encoding="utf-8"))
        result["capture_stats"] = capture_stats

    result["case"] = case.name
    metadata = json.loads((case / "result.json").read_text(encoding="utf-8"))
    requested_l4s_fraction = float(metadata["requested_l4s_byte_fraction"])
    result["experiment"] = {
        "requested_enhancement_l4s_fraction": metadata.get(
            "requested_enhancement_l4s_fraction"
        ),
        "actual_enhancement_l4s_fraction": metadata.get(
            "actual_enhancement_l4s_fraction"
        ),
        "actual_total_l4s_byte_fraction": metadata.get(
            "actual_total_l4s_byte_fraction"
        ),
        "base_byte_fraction": metadata.get("base_byte_fraction"),
        "downstream_mode": metadata.get("downstream_mode"),
    }
    result["acceptance"] = evaluate_acceptance(
        result,
        requested_l4s_fraction=requested_l4s_fraction,
        capture_stats=capture_stats,
        minimum_provider_match_fraction=minimum_provider_match_fraction,
        minimum_downstream_match_fraction=minimum_downstream_match_fraction,
    )
    destination = case / "reordering-analysis.json"
    destination.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--server-ip", default="10.0.0.1")
    parser.add_argument("--minimum-provider-match-fraction", type=float, default=0.95)
    parser.add_argument("--minimum-downstream-match-fraction", type=float, default=0.95)
    args = parser.parse_args()
    for value in (
        args.minimum_provider_match_fraction,
        args.minimum_downstream_match_fraction,
    ):
        if not 0.0 <= value <= 1.0:
            parser.error("match fractions must be in [0,1]")
    cases = sorted(
        path for path in args.root.iterdir()
        if path.is_dir() and path.name.startswith("l4s-")
    )
    if not cases:
        raise SystemExit(f"no l4s-* case directories under {args.root}")
    results = [
        analyze_case(
            case,
            server_ip=args.server_ip,
            minimum_provider_match_fraction=args.minimum_provider_match_fraction,
            minimum_downstream_match_fraction=args.minimum_downstream_match_fraction,
        )
        for case in cases
    ]
    (args.root / "reordering-analysis.json").write_text(
        json.dumps(
            {
                "scenario": "3dgs-partial-l4s-post-send-reordering-v2",
                "cases": results,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    invalid = [
        result["case"] for result in results
        if result["acceptance"]["status"] != "pass"
    ]
    if invalid:
        raise SystemExit(f"measurement validity failed for: {', '.join(invalid)}")


if __name__ == "__main__":
    main()
