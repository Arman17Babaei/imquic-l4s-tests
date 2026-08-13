#!/usr/bin/env python3
"""Analyze and plot the ten-phase independent Reno step-join experiment."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import subprocess
import tempfile
from typing import Sequence

from analyze_reno_fairness import (
    AnalysisError,
    PacketSample,
    TransportSample,
    parse_iperf_json,
)
from reno_step_join_common import PHASE_LABELS, STREAM_COUNT, STREAMS, phase_boundaries
from visualization import plot_step_join_timeline

DEFAULT_BIN_SECONDS = 0.25
TIMELINE_FIELDS = (
    "time_s", "phase", "stream", "active", "throughput_mbps", "cwnd_bytes",
)
AGGREGATE_FIELDS = (
    "time_s", "phase", "stream", "active_repetitions",
    "throughput_mbps_mean", "throughput_mbps_stdev",
    "cwnd_bytes_mean", "cwnd_bytes_stdev",
)


def read_events(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as input_file:
        rows = list(csv.DictReader(input_file))
    if not rows:
        raise AnalysisError(f"{path}: no events")
    for row in rows:
        row["time_s"] = float(row["time_s"])
        row["scheduled_time_s"] = float(row["scheduled_time_s"])
        row["status"] = int(row["status"]) if row.get("status") not in ("", None) else None
    return rows


def validate_events(events: Sequence[dict], phase_seconds: float,
                    early_tolerance: float = 0.1) -> dict[str, float]:
    start_rows = [row for row in events if row["event"] == "start"]
    end_rows = [row for row in events if row["event"] == "end"]
    starts = {row["stream"]: float(row["time_s"]) for row in start_rows}
    ends = {row["stream"]: row for row in end_rows}
    expected = {stream.name for stream in STREAMS}
    if (set(starts) != expected or set(ends) != expected
            or len(start_rows) != STREAM_COUNT or len(end_rows) != STREAM_COUNT):
        raise AnalysisError("events do not contain exactly one start/end for every stream")
    failed = {name: row["status"] for name, row in ends.items() if row["status"] != 0}
    if failed:
        raise AnalysisError(f"iperf3 clients failed: {failed}")
    endpoint = STREAM_COUNT * phase_seconds
    if any(float(row["time_s"]) + early_tolerance < endpoint for row in ends.values()):
        raise AnalysisError("one or more streams ended before the experiment endpoint")
    return starts


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
    streams = {int(row["client_port"]): row["stream"] for row in metadata["streams"]}
    cport_filter = " || ".join(f"tcp.srcport == {port}" for port in sorted(streams))
    display_filter = f"ip.dst == {metadata['server_ip']} && tcp && ({cport_filter})"
    fields = ("frame.time_epoch", "ip.len", "tcp.srcport", "ip.dsfield.ecn")
    text = _tshark_fields(path, display_filter, fields, runner=runner)
    start_epoch = float(metadata["experiment_started_epoch"])
    samples = []
    for number, line in enumerate(text.splitlines(), 1):
        values = line.split("\t")
        if len(values) != len(fields):
            raise AnalysisError(f"{path}: malformed tshark field row {number}: {line!r}")
        if not values[0] or not values[1] or not values[2]:
            continue
        port = int(values[2].split(",", 1)[0])
        if port not in streams:
            continue
        samples.append(PacketSample(
            time_s=float(values[0]) - start_epoch,
            ip_bytes=int(values[1].split(",", 1)[0]),
            instance=streams[port],
            ecn=int(values[3].split(",", 1)[0] if values[3] else "0", 0),
            retransmission=False,
        ))
    return samples


def _latest_transport(samples: Sequence[TransportSample], time_s: float) -> TransportSample | None:
    latest = None
    for sample in samples:
        if sample.time_s > time_s:
            break
        latest = sample
    return latest


def build_timeline(metadata: dict, packets: Sequence[PacketSample],
                   transports: dict[str, Sequence[TransportSample]],
                   bin_seconds: float = DEFAULT_BIN_SECONDS) -> list[dict]:
    if bin_seconds <= 0:
        raise ValueError("bin duration must be positive")
    phase_seconds = float(metadata["phase_seconds"])
    duration = STREAM_COUNT * phase_seconds
    bins = max(1, int(math.ceil(duration / bin_seconds)))
    packet_bytes = {stream.name: [0] * bins for stream in STREAMS}
    for packet in packets:
        index = int(packet.time_s / bin_seconds)
        if 0 <= index < bins:
            packet_bytes[packet.instance][index] += packet.ip_bytes

    rows = []
    for index in range(bins):
        center = (index + 0.5) * bin_seconds
        phase = min(STREAM_COUNT, int(center / phase_seconds) + 1)
        for stream in STREAMS:
            active = stream.start_seconds(phase_seconds) <= center < duration
            transport = (
                _latest_transport(transports.get(stream.name, ()), center)
                if active else None
            )
            rows.append({
                "time_s": center,
                "phase": phase,
                "stream": stream.name,
                "active": int(active),
                "throughput_mbps": (
                    packet_bytes[stream.name][index] * 8 / bin_seconds / 1e6 if active else 0.0
                ),
                "cwnd_bytes": transport.cwnd_bytes if transport else None,
            })
    return rows


def write_rows(path: Path, rows: Sequence[dict], fields: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "" if row.get(field) is None else row.get(field)
                             for field in fields})


def aggregate_timelines(timelines: Sequence[Sequence[dict]],
                        expected_repetitions: int) -> list[dict]:
    if len(timelines) != expected_repetitions:
        raise AnalysisError(
            f"expected {expected_repetitions} repetition timelines, found {len(timelines)}"
        )
    indexed = []
    for rows in timelines:
        mapping = {(float(row["time_s"]), row["stream"]): row for row in rows}
        if len(mapping) != len(rows):
            raise AnalysisError("timeline has duplicate time/stream rows")
        indexed.append(mapping)
    keys = set(indexed[0])
    if any(set(mapping) != keys for mapping in indexed[1:]):
        raise AnalysisError("repetition timelines are not aligned")

    result = []
    for time_s, stream in sorted(keys):
        samples = [mapping[(time_s, stream)] for mapping in indexed]
        active = [row for row in samples if int(row["active"]) == 1]
        rates = [float(row["throughput_mbps"]) for row in active]
        cwnds = [float(row["cwnd_bytes"]) for row in active
                 if row.get("cwnd_bytes") not in (None, "")]
        complete_cwnds = cwnds if len(cwnds) == len(active) else []
        result.append({
            "time_s": time_s,
            "phase": int(samples[0]["phase"]),
            "stream": stream,
            "active_repetitions": len(active),
            "throughput_mbps_mean": statistics.mean(rates) if rates else None,
            "throughput_mbps_stdev": (
                statistics.stdev(rates) if len(rates) > 1
                else (0.0 if rates else None)
            ),
            "cwnd_bytes_mean": statistics.mean(complete_cwnds) if complete_cwnds else None,
            "cwnd_bytes_stdev": (
                statistics.stdev(complete_cwnds) if len(complete_cwnds) > 1
                else (0.0 if complete_cwnds else None)
            ),
        })
    return result


def analyze_case(case: Path, bin_seconds: float,
                 expected_congestion: str | None = None) -> list[dict]:
    metadata = json.loads((case / "metadata.json").read_text(encoding="utf-8"))
    if int(metadata.get("phase_count", 0)) != STREAM_COUNT:
        raise AnalysisError(f"{case}: expected {STREAM_COUNT} phases")
    if len(metadata.get("streams", [])) != STREAM_COUNT:
        raise AnalysisError(f"{case}: expected {STREAM_COUNT} stream metadata records")
    names = [row["stream"] for row in metadata["streams"]]
    client_ports = [int(row["client_port"]) for row in metadata["streams"]]
    server_ports = [int(row["server_port"]) for row in metadata["streams"]]
    if set(names) != {stream.name for stream in STREAMS}:
        raise AnalysisError(f"{case}: stream identifiers do not match the schedule")
    if len(set(client_ports)) != STREAM_COUNT or len(set(server_ports)) != STREAM_COUNT:
        raise AnalysisError(f"{case}: client and server ports must each be unique")
    events = read_events(case / "events.csv")
    starts = validate_events(events, float(metadata["phase_seconds"]))
    flow_meta = {row["stream"]: row for row in metadata["streams"]}
    congestion = str(metadata["tcp_congestion"])
    if expected_congestion is not None and congestion != expected_congestion:
        raise AnalysisError(
            f"{case}: congestion controller {congestion!r} does not match "
            f"experiment controller {expected_congestion!r}"
        )
    transports = {
        stream.name: parse_iperf_json(
            case / f"iperf_{stream.name}.json",
            flow_meta[stream.name],
            starts[stream.name],
            congestion,
        )
        for stream in STREAMS
    }
    packets = parse_pcap(case / "bottleneck.pcap", metadata)
    if any(packet.ecn != 0 for packet in packets):
        raise AnalysisError(f"{case}: Not-ECT invariant failed")
    missing = [stream.name for stream in STREAMS
               if not any(packet.instance == stream.name for packet in packets)]
    if missing:
        raise AnalysisError(f"{case}: capture contains no data packets for {missing}")
    rows = build_timeline(metadata, packets, transports, bin_seconds)
    write_rows(case / "timeline.csv", rows, TIMELINE_FIELDS)
    boundaries = phase_boundaries(float(metadata["phase_seconds"]))
    plot_step_join_timeline(
        rows, boundaries, PHASE_LABELS, float(metadata["bottleneck_mbps"]),
        case / "timeline", run_label=case.name,
        congestion_label=congestion.upper(),
    )
    return rows


def analyze(root: Path, bin_seconds: float) -> list[dict]:
    experiment = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    if int(experiment.get("phase_count", 0)) != STREAM_COUNT:
        raise AnalysisError(f"expected a {STREAM_COUNT}-phase experiment")
    repetitions = int(experiment["repetitions"])
    cases = sorted(path for path in root.glob("rep_*") if path.is_dir())
    if len(cases) != repetitions:
        raise AnalysisError(f"expected {repetitions} repetitions, found {len(cases)}")
    congestion = str(experiment["tcp_congestion"])
    timelines = [analyze_case(case, bin_seconds, congestion) for case in cases]
    aggregate = aggregate_timelines(timelines, repetitions)
    write_rows(root / "aggregate_timeline.csv", aggregate, AGGREGATE_FIELDS)
    boundaries = phase_boundaries(float(experiment["phase_seconds"]))
    plot_step_join_timeline(
        aggregate, boundaries, PHASE_LABELS, float(experiment["bottleneck_mbps"]),
        root / "aggregate_timeline", aggregate=True,
        congestion_label=str(experiment["tcp_congestion"]).upper(),
    )
    result = {
        "repetitions": repetitions,
        "streams": STREAM_COUNT,
        "tcp_congestion": str(experiment["tcp_congestion"]),
        "phase_seconds": float(experiment["phase_seconds"]),
        "throughput_bin_ms": bin_seconds * 1000,
        "throughput_source": "bottleneck pcap IPv4 ip.len",
        "cwnd_source": "iperf3 sender TCP_INFO snd_cwnd",
        "aggregation": "per-time/per-stream mean and sample standard deviation",
        "valid": True,
    }
    (root / "analysis.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return aggregate


def _synthetic_timeline(offset: float = 0.0, phase_seconds: float = 1.0) -> list[dict]:
    rows = []
    for tick in range(STREAM_COUNT * 4):
        time_s = (tick + 0.5) * 0.25
        phase = int(time_s / phase_seconds) + 1
        for stream in STREAMS:
            active = time_s >= stream.start_seconds(phase_seconds)
            rows.append({
                "time_s": time_s,
                "phase": phase,
                "stream": stream.name,
                "active": int(active),
                "throughput_mbps": stream.number + offset if active else 0.0,
                "cwnd_bytes": (stream.number * 1024 + offset * 1024) if active else None,
            })
    return rows


def self_test() -> None:
    first, second, third = (_synthetic_timeline(value) for value in (0.0, 1.0, 2.0))
    aggregate = aggregate_timelines((first, second, third), 3)
    row = next(item for item in aggregate
               if item["stream"] == "stream_01" and item["active_repetitions"] == 3)
    assert row["throughput_mbps_mean"] == 2.0
    assert row["throughput_mbps_stdev"] == 1.0
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_rows(root / "aggregate.csv", aggregate, AGGREGATE_FIELDS)
        plot_step_join_timeline(
            aggregate, phase_boundaries(1), PHASE_LABELS, 20,
            root / "aggregate", aggregate=True,
        )
        assert (root / "aggregate.svg").stat().st_size > 1000
        assert (root / "aggregate.png").stat().st_size > 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path)
    parser.add_argument("--bin-seconds", type=float, default=DEFAULT_BIN_SECONDS)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("Reno step-join analyzer self-test: PASS")
        return
    if args.root is None:
        parser.error("root is required unless --self-test is used")
    if args.bin_seconds <= 0:
        parser.error("bin-seconds must be positive")
    analyze(args.root, args.bin_seconds)


if __name__ == "__main__":
    main()
