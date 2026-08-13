#!/usr/bin/env python3
"""Analyze the five-phase Reno/Reno fairness diagnostic.

The analyzer turns raw iperf3 JSON + one bottleneck pcap into a canonical
250-ms long-form timeline, per-run mechanism summaries, and standardized
SVG/PNG figures.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
import subprocess
import tempfile
from typing import Sequence

from reno_fairness_common import (
    FLOW_INSTANCES,
    PHASE_LABELS,
    convergence_time,
    jain_fairness,
)
from visualization import (
    plot_convergence_comparison,
    plot_fairness_timeline,
    plot_repetition_phase_summary,
    plot_solo_check,
)

DEFAULT_BIN_SECONDS = 0.25
DEFAULT_TRANSIENT_SECONDS = 3.0
DEFAULT_FAIR_LOW = 0.45
DEFAULT_FAIR_HIGH = 0.55
DEFAULT_CONVERGENCE_BINS = 3
DEFAULT_SOLO_TOLERANCE = 0.10
TIMELINE_FIELDS = (
    "time_s",
    "flow",
    "instance",
    "phase",
    "active",
    "throughput_mbps",
    "flow_share",
    "cwnd_bytes",
    "rtt_ms",
    "rttvar_ms",
    "retransmissions",
    "iperf_retransmits",
)
SUMMARY_FIELDS = (
    "repetition",
    "solo_A_before_mbps",
    "solo_B_mbps",
    "solo_A_after_mbps",
    "phase2_A_share",
    "phase4_A_share",
    "phase2_incumbent_share",
    "phase4_incumbent_share",
    "incumbency_effect",
    "identity_effect",
    "phase2_jain",
    "phase4_jain",
    "phase2_convergence_s",
    "phase4_convergence_s",
    "phase2_retrans_A",
    "phase2_retrans_B",
    "phase4_retrans_A",
    "phase4_retrans_B",
    "phase2_mean_cwnd_A",
    "phase2_mean_cwnd_B",
    "phase4_mean_cwnd_A",
    "phase4_mean_cwnd_B",
    "phase2_mean_rtt_A_ms",
    "phase2_mean_rtt_B_ms",
    "phase4_mean_rtt_A_ms",
    "phase4_mean_rtt_B_ms",
    "valid",
    "issues",
)


class AnalysisError(RuntimeError):
    pass


@dataclass(frozen=True)
class PacketSample:
    time_s: float
    ip_bytes: int
    instance: str
    ecn: int
    retransmission: bool


@dataclass(frozen=True)
class TransportSample:
    time_s: float
    cwnd_bytes: int
    rtt_ms: float
    rttvar_ms: float
    retransmits: int


def read_events(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise AnalysisError(f"{path}: no events")
    for row in rows:
        row["time_s"] = float(row["time_s"])
        row["scheduled_time_s"] = float(row["scheduled_time_s"])
        row["status"] = int(row["status"]) if row.get("status") not in ("", None) else None
    return rows


def actual_phase_boundaries(events: Sequence[dict]) -> list[float]:
    lookup = {(row["instance"], row["event"]): float(row["time_s"]) for row in events}
    keys = (
        ("A1", "start"),
        ("B1", "start"),
        ("A1", "end"),
        ("A2", "start"),
        ("B1", "end"),
        ("A2", "end"),
    )
    try:
        boundaries = [lookup[key] for key in keys]
    except KeyError as error:
        raise AnalysisError(f"missing schedule event: {error.args[0]}") from error
    if any(after <= before for before, after in zip(boundaries, boundaries[1:])):
        raise AnalysisError(f"invalid phase ordering: {boundaries}")
    return boundaries


def parse_iperf_json(path: Path, expected: dict, actual_start_s: float,
                     expected_congestion: str = "reno") -> list[TransportSample]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisError(f"{path}: invalid iperf3 JSON: {error}") from error
    start = data.get("start", {})
    test_start = start.get("test_start", {})
    if test_start.get("protocol") != "TCP":
        raise AnalysisError(f"{path}: iperf3 test is not TCP")
    if int(test_start.get("num_streams", 0)) != 1:
        raise AnalysisError(f"{path}: expected exactly one iperf3 stream")
    if int(test_start.get("target_bitrate", 0) or 0) != 0:
        raise AnalysisError(f"{path}: TCP iperf3 stream was application-rate-limited")
    connected = start.get("connected", [])
    if len(connected) != 1:
        raise AnalysisError(f"{path}: expected one iperf3 data connection")
    connection = connected[0]
    if int(connection.get("local_port", -1)) != int(expected["client_port"]):
        raise AnalysisError(
            f"{path}: data cport {connection.get('local_port')} != {expected['client_port']}"
        )
    if int(connection.get("remote_port", -1)) != int(expected["server_port"]):
        raise AnalysisError(
            f"{path}: server port {connection.get('remote_port')} != {expected['server_port']}"
        )
    congestion = data.get("end", {}).get("sender_tcp_congestion")
    if congestion != expected_congestion:
        raise AnalysisError(
            f"{path}: sender congestion control is {congestion!r}, "
            f"expected {expected_congestion!r}"
        )

    samples = []
    for interval in data.get("intervals", []):
        streams = interval.get("streams", [])
        if len(streams) != 1:
            raise AnalysisError(f"{path}: interval does not contain exactly one stream")
        stream = streams[0]
        required = ("end", "snd_cwnd", "rtt", "rttvar", "retransmits")
        missing = [field for field in required if field not in stream]
        if missing:
            raise AnalysisError(f"{path}: interval missing Linux TCP_INFO fields: {missing}")
        samples.append(TransportSample(
            time_s=actual_start_s + float(stream["end"]),
            cwnd_bytes=int(stream["snd_cwnd"]),
            rtt_ms=float(stream["rtt"]) / 1000.0,
            rttvar_ms=float(stream["rttvar"]) / 1000.0,
            retransmits=int(stream["retransmits"]),
        ))
    if not samples:
        raise AnalysisError(f"{path}: no iperf3 intervals")
    return samples


def _tshark_fields(path: Path, display_filter: str, fields: Sequence[str],
                   runner=subprocess.run) -> str:
    args = ["tshark", "-r", str(path), "-Y", display_filter, "-T", "fields"]
    for field in fields:
        args.extend(("-e", field))
    args.extend(("-E", "occurrence=f"))
    result = runner(args, check=False, text=True, capture_output=True)
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 2 and "appears to have been cut short" in result.stderr:
        return result.stdout
    raise AnalysisError(f"{path}: tshark failed ({result.returncode}): {result.stderr.strip()}")


def parse_pcap(path: Path, metadata: dict, runner=subprocess.run) -> list[PacketSample]:
    flows = {int(row["client_port"]): row["instance"] for row in metadata["flows"]}
    cport_filter = " || ".join(f"tcp.srcport == {port}" for port in sorted(flows))
    display_filter = f"ip.dst == {metadata['server_ip']} && tcp && ({cport_filter})"
    fields = (
        "frame.time_epoch",
        "ip.len",
        "tcp.srcport",
        "tcp.dstport",
        "ip.dsfield.ecn",
        "tcp.analysis.retransmission",
        "tcp.analysis.fast_retransmission",
    )
    text = _tshark_fields(path, display_filter, fields, runner=runner)
    samples = []
    start_epoch = float(metadata["experiment_started_epoch"])
    for number, line in enumerate(text.splitlines(), 1):
        values = line.split("\t")
        if len(values) != len(fields):
            raise AnalysisError(f"{path}: malformed tshark field row {number}: {line!r}")
        if not values[0] or not values[1] or not values[2]:
            continue
        src_port = int(values[2].split(",", 1)[0])
        if src_port not in flows:
            continue
        ecn_text = values[4].split(",", 1)[0] if values[4] else "0"
        samples.append(PacketSample(
            time_s=float(values[0]) - start_epoch,
            ip_bytes=int(values[1].split(",", 1)[0]),
            instance=flows[src_port],
            ecn=int(ecn_text, 0),
            retransmission=bool(values[5] or values[6]),
        ))
    return samples


def _latest_transport(samples: Sequence[TransportSample], time_s: float) -> TransportSample | None:
    latest = None
    for sample in samples:
        if sample.time_s > time_s:
            break
        latest = sample
    return latest


def _phase_at(time_s: float, boundaries: Sequence[float]) -> int | None:
    for index in range(5):
        if boundaries[index] <= time_s < boundaries[index + 1]:
            return index
    return None


def build_timeline(metadata: dict, events: Sequence[dict], packets: Sequence[PacketSample],
                   transports: dict[str, Sequence[TransportSample]],
                   bin_seconds: float = DEFAULT_BIN_SECONDS) -> tuple[list[dict], list[float]]:
    if bin_seconds <= 0:
        raise ValueError("bin duration must be positive")
    boundaries = actual_phase_boundaries(events)
    start, end = boundaries[0], boundaries[-1]
    bins = max(1, int(math.ceil((end - start) / bin_seconds)))
    packet_bytes = {instance.instance: [0] * bins for instance in FLOW_INSTANCES}
    retrans = {instance.instance: [0] * bins for instance in FLOW_INSTANCES}
    for packet in packets:
        index = int((packet.time_s - start) / bin_seconds)
        if 0 <= index < bins:
            packet_bytes[packet.instance][index] += packet.ip_bytes
            if packet.retransmission:
                retrans[packet.instance][index] += 1

    rows = []
    for index in range(bins):
        center = start + (index + 0.5) * bin_seconds
        phase = _phase_at(center, boundaries)
        if phase is None:
            continue
        rates = {}
        for logical_flow in ("A", "B"):
            instances = [flow.instance for flow in FLOW_INSTANCES if flow.logical_flow == logical_flow]
            total_bytes = sum(packet_bytes[instance][index] for instance in instances)
            rates[logical_flow] = total_bytes * 8 / bin_seconds / 1e6
        combined = rates["A"] + rates["B"]
        shares = {"A": None, "B": None}
        if phase in (1, 3) and combined > 0:
            shares["A"] = rates["A"] / combined
            shares["B"] = rates["B"] / combined

        active_instance = {
            "A": "A1" if phase in (0, 1) else ("A2" if phase in (3, 4) else ""),
            "B": "B1" if phase in (1, 2, 3) else "",
        }
        for logical_flow in ("A", "B"):
            instance = active_instance[logical_flow]
            transport = _latest_transport(transports.get(instance, ()), center) if instance else None
            rows.append({
                "time_s": center,
                "flow": logical_flow,
                "instance": instance,
                "phase": PHASE_LABELS[phase],
                "active": int(bool(instance)),
                "throughput_mbps": rates[logical_flow],
                "flow_share": shares[logical_flow],
                "cwnd_bytes": transport.cwnd_bytes if transport else None,
                "rtt_ms": transport.rtt_ms if transport else None,
                "rttvar_ms": transport.rttvar_ms if transport else None,
                "retransmissions": (
                    retrans[instance][index] if instance else 0
                ),
                "iperf_retransmits": transport.retransmits if transport else None,
            })
    return rows, boundaries


def write_timeline(path: Path, rows: Sequence[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=TIMELINE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "" if row.get(field) is None else row.get(field)
                             for field in TIMELINE_FIELDS})


def _window(rows: Sequence[dict], flow: str, start: float, end: float) -> list[dict]:
    return [row for row in rows if row["flow"] == flow and start <= float(row["time_s"]) < end]


def _mean(rows: Sequence[dict], field: str) -> float:
    values = [float(row[field]) for row in rows if row.get(field) not in (None, "")]
    return statistics.mean(values) if values else math.nan


def _sum(rows: Sequence[dict], field: str) -> int:
    return sum(int(float(row[field])) for row in rows if row.get(field) not in (None, ""))


def _steady_window(start: float, end: float, transient_seconds: float) -> tuple[float, float]:
    duration = end - start
    transient = min(transient_seconds, max(0.0, duration / 3.0))
    return start + transient, end


def compute_summary_from_rows(rows: Sequence[dict], boundaries: Sequence[float],
                              repetition: int = 1,
                              transient_seconds: float = DEFAULT_TRANSIENT_SECONDS,
                              fair_low: float = DEFAULT_FAIR_LOW,
                              fair_high: float = DEFAULT_FAIR_HIGH,
                              convergence_bins: int = DEFAULT_CONVERGENCE_BINS,
                              solo_tolerance: float = DEFAULT_SOLO_TOLERANCE) -> dict:
    steady = [_steady_window(boundaries[index], boundaries[index + 1], transient_seconds)
              for index in range(5)]
    solo_a_before = _mean(_window(rows, "A", *steady[0]), "throughput_mbps")
    solo_b = _mean(_window(rows, "B", *steady[2]), "throughput_mbps")
    solo_a_after = _mean(_window(rows, "A", *steady[4]), "throughput_mbps")

    phase2_a = _window(rows, "A", *steady[1])
    phase2_b = _window(rows, "B", *steady[1])
    phase4_a = _window(rows, "A", *steady[3])
    phase4_b = _window(rows, "B", *steady[3])
    phase2_a_share = _mean(phase2_a, "flow_share")
    phase4_a_share = _mean(phase4_a, "flow_share")
    phase2_incumbent = phase2_a_share
    phase4_incumbent = 1 - phase4_a_share
    incumbency_effect = statistics.mean((phase2_incumbent, phase4_incumbent)) - 0.5
    identity_effect = statistics.mean((phase2_a_share, phase4_a_share)) - 0.5

    a_overlap_rows = [row for row in rows if row["flow"] == "A"]
    times = [float(row["time_s"]) for row in a_overlap_rows]
    shares = [None if row.get("flow_share") in (None, "") else float(row["flow_share"])
              for row in a_overlap_rows]
    phase2_shares = [share if boundaries[1] <= time_s < boundaries[2] else None
                     for time_s, share in zip(times, shares)]
    phase4_shares = [share if boundaries[3] <= time_s < boundaries[4] else None
                     for time_s, share in zip(times, shares)]
    phase2_convergence = convergence_time(
        times, phase2_shares, boundaries[1], fair_low, fair_high, convergence_bins
    )
    phase4_convergence = convergence_time(
        times, phase4_shares, boundaries[3], fair_low, fair_high, convergence_bins
    )

    issues = []
    solo_values = [solo_a_before, solo_b, solo_a_after]
    if all(math.isfinite(value) for value in solo_values):
        solo_mean = statistics.mean(solo_values)
        if solo_mean <= 0:
            issues.append("solo flows carried no traffic")
        elif max(abs(value - solo_mean) / solo_mean for value in solo_values) > solo_tolerance:
            issues.append("BASELINE_ASYMMETRY")
    else:
        issues.append("missing solo-flow throughput")

    phase2_rate_a, phase2_rate_b = _mean(phase2_a, "throughput_mbps"), _mean(phase2_b, "throughput_mbps")
    phase4_rate_a, phase4_rate_b = _mean(phase4_a, "throughput_mbps"), _mean(phase4_b, "throughput_mbps")
    result = {
        "repetition": repetition,
        "solo_A_before_mbps": solo_a_before,
        "solo_B_mbps": solo_b,
        "solo_A_after_mbps": solo_a_after,
        "phase2_A_share": phase2_a_share,
        "phase4_A_share": phase4_a_share,
        "phase2_incumbent_share": phase2_incumbent,
        "phase4_incumbent_share": phase4_incumbent,
        "incumbency_effect": incumbency_effect,
        "identity_effect": identity_effect,
        "phase2_jain": jain_fairness((phase2_rate_a, phase2_rate_b)),
        "phase4_jain": jain_fairness((phase4_rate_a, phase4_rate_b)),
        "phase2_convergence_s": phase2_convergence,
        "phase4_convergence_s": phase4_convergence,
        "phase2_retrans_A": _sum(phase2_a, "retransmissions"),
        "phase2_retrans_B": _sum(phase2_b, "retransmissions"),
        "phase4_retrans_A": _sum(phase4_a, "retransmissions"),
        "phase4_retrans_B": _sum(phase4_b, "retransmissions"),
        "phase2_mean_cwnd_A": _mean(phase2_a, "cwnd_bytes"),
        "phase2_mean_cwnd_B": _mean(phase2_b, "cwnd_bytes"),
        "phase4_mean_cwnd_A": _mean(phase4_a, "cwnd_bytes"),
        "phase4_mean_cwnd_B": _mean(phase4_b, "cwnd_bytes"),
        "phase2_mean_rtt_A_ms": _mean(phase2_a, "rtt_ms"),
        "phase2_mean_rtt_B_ms": _mean(phase2_b, "rtt_ms"),
        "phase4_mean_rtt_A_ms": _mean(phase4_a, "rtt_ms"),
        "phase4_mean_rtt_B_ms": _mean(phase4_b, "rtt_ms"),
        "valid": not issues,
        "issues": issues,
    }
    return result


def analyze_case(case: Path, bin_seconds: float, transient_seconds: float,
                 plot_mode: str) -> dict:
    metadata = json.loads((case / "metadata.json").read_text(encoding="utf-8"))
    events = read_events(case / "events.csv")
    boundaries = actual_phase_boundaries(events)
    starts = {row["instance"]: row["time_s"] for row in events if row["event"] == "start"}
    transports = {}
    flow_meta = {row["instance"]: row for row in metadata["flows"]}
    for flow in FLOW_INSTANCES:
        transports[flow.instance] = parse_iperf_json(
            case / f"iperf_{flow.instance}.json",
            flow_meta[flow.instance],
            starts[flow.instance],
        )
    packets = parse_pcap(case / "bottleneck.pcap", metadata)
    issues = []
    if any(packet.ecn != 0 for packet in packets):
        issues.append("Not-ECT invariant failed: capture contains ECT/CE packets")
    for flow in FLOW_INSTANCES:
        if not any(packet.instance == flow.instance for packet in packets):
            issues.append(f"capture contains no data packets for {flow.instance}")
    rows, boundaries = build_timeline(metadata, events, packets, transports, bin_seconds)
    write_timeline(case / "timeline.csv", rows)
    summary = compute_summary_from_rows(
        rows,
        boundaries,
        repetition=int(metadata["repetition"]),
        transient_seconds=transient_seconds,
    )
    summary["issues"] = issues + list(summary["issues"])
    summary["valid"] = not summary["issues"]
    (case / "summary.json").write_text(
        json.dumps(_json_safe(summary), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    plot_fairness_timeline(
        rows,
        events,
        boundaries,
        PHASE_LABELS,
        float(metadata["bottleneck_mbps"]),
        case / "timeline",
        plot_mode=plot_mode,
        run_label=case.name,
    )
    plot_convergence_comparison(rows, boundaries, case / "convergence", plot_mode=plot_mode)
    return summary


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(value)
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def write_summary_csv(path: Path, summaries: Sequence[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for summary in summaries:
            writer.writerow({field: _csv_value(summary.get(field)) for field in SUMMARY_FIELDS})


def aggregate_summaries(summaries: Sequence[dict]) -> dict:
    metrics = (
        "solo_A_before_mbps", "solo_B_mbps", "solo_A_after_mbps",
        "phase2_A_share", "phase4_A_share",
        "phase2_incumbent_share", "phase4_incumbent_share",
        "incumbency_effect", "identity_effect", "phase2_jain", "phase4_jain",
    )
    aggregate = {
        "repetitions": len(summaries),
        "valid_repetitions": sum(bool(row["valid"]) for row in summaries),
    }
    for metric in metrics:
        values = [float(row[metric]) for row in summaries
                  if row.get(metric) is not None and math.isfinite(float(row[metric]))]
        aggregate[f"{metric}_mean"] = statistics.mean(values) if values else None
        aggregate[f"{metric}_stdev"] = statistics.stdev(values) if len(values) > 1 else 0.0
    return aggregate


def analyze(root: Path, bin_seconds: float, transient_seconds: float,
            plot_mode: str) -> list[dict]:
    experiment = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    cases = sorted(path for path in root.glob("rep_*") if path.is_dir())
    if len(cases) != int(experiment["repetitions"]):
        raise AnalysisError(
            f"expected {experiment['repetitions']} repetitions, found {len(cases)}"
        )
    summaries = [analyze_case(case, bin_seconds, transient_seconds, plot_mode) for case in cases]
    write_summary_csv(root / "summary.csv", summaries)
    aggregate = aggregate_summaries(summaries)
    (root / "aggregate.json").write_text(
        json.dumps(aggregate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    analysis_metadata = {
        "throughput_bin_ms": bin_seconds * 1000,
        "throughput_source": "bottleneck pcap IPv4 ip.len",
        "transient_window_s": transient_seconds,
        "fair_share_low": DEFAULT_FAIR_LOW,
        "fair_share_high": DEFAULT_FAIR_HIGH,
        "convergence_bins": DEFAULT_CONVERGENCE_BINS,
        "solo_tolerance_fraction": DEFAULT_SOLO_TOLERANCE,
        "plot_mode": plot_mode,
        "plot_version": 1,
    }
    (root / "analysis_metadata.json").write_text(
        json.dumps(analysis_metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    plot_repetition_phase_summary(summaries, root / "phase_summary")
    plot_solo_check(summaries, root / "solo_check", float(experiment["bottleneck_mbps"]))
    return summaries


def _synthetic_rows(phase_seconds: float = 15.0, phase2_a: float = 0.5,
                    phase4_a: float = 0.5) -> tuple[list[dict], list[float]]:
    boundaries = [index * phase_seconds for index in range(6)]
    rows = []
    for tick in range(int(boundaries[-1] / DEFAULT_BIN_SECONDS)):
        time_s = (tick + 0.5) * DEFAULT_BIN_SECONDS
        phase = _phase_at(time_s, boundaries)
        if phase is None:
            continue
        if phase == 0:
            rates = {"A": 20.0, "B": 0.0}
        elif phase == 1:
            rates = {"A": 20 * phase2_a, "B": 20 * (1 - phase2_a)}
        elif phase == 2:
            rates = {"A": 0.0, "B": 20.0}
        elif phase == 3:
            rates = {"A": 20 * phase4_a, "B": 20 * (1 - phase4_a)}
        else:
            rates = {"A": 20.0, "B": 0.0}
        for flow in ("A", "B"):
            instance = "A1" if flow == "A" and phase in (0, 1) else (
                "A2" if flow == "A" and phase in (3, 4) else (
                    "B1" if flow == "B" and phase in (1, 2, 3) else ""
                )
            )
            combined = rates["A"] + rates["B"]
            share = rates[flow] / combined if phase in (1, 3) and combined else None
            rows.append({
                "time_s": time_s,
                "flow": flow,
                "instance": instance,
                "phase": PHASE_LABELS[phase],
                "active": int(bool(instance)),
                "throughput_mbps": rates[flow],
                "flow_share": share,
                "cwnd_bytes": 64000 if instance else None,
                "rtt_ms": 15.0 if instance else None,
                "rttvar_ms": 1.0 if instance else None,
                "retransmissions": 0,
                "iperf_retransmits": 0 if instance else None,
            })
    return rows, boundaries


def self_test() -> None:
    rows, boundaries = _synthetic_rows()
    fair = compute_summary_from_rows(rows, boundaries)
    assert abs(fair["incumbency_effect"]) < 1e-9
    assert abs(fair["identity_effect"]) < 1e-9
    incumbent_rows, boundaries = _synthetic_rows(phase2_a=0.7, phase4_a=0.3)
    incumbent = compute_summary_from_rows(incumbent_rows, boundaries)
    assert incumbent["incumbency_effect"] > 0.19
    assert abs(incumbent["identity_effect"]) < 1e-9
    identity_rows, boundaries = _synthetic_rows(phase2_a=0.7, phase4_a=0.7)
    identity = compute_summary_from_rows(identity_rows, boundaries)
    assert identity["identity_effect"] > 0.19
    assert abs(identity["incumbency_effect"]) < 1e-9
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "summary.csv"
        write_summary_csv(path, (fair, incumbent, identity))
        assert path.stat().st_size > 0
    print("Reno fairness analyzer self-test: PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=Path)
    parser.add_argument("--bin-ms", type=float, default=DEFAULT_BIN_SECONDS * 1000)
    parser.add_argument("--transient-seconds", type=float, default=DEFAULT_TRANSIENT_SECONDS)
    parser.add_argument("--plot-mode", choices=("debug", "paper"), default="debug")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.directory is None:
        parser.error("directory is required unless --self-test is used")
    if args.bin_ms <= 0:
        parser.error("bin-ms must be positive")
    if args.transient_seconds < 0:
        parser.error("transient-seconds must be non-negative")
    analyze(args.directory, args.bin_ms / 1000.0, args.transient_seconds, args.plot_mode)


if __name__ == "__main__":
    main()
