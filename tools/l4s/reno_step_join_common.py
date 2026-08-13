#!/usr/bin/env python3
"""Pure schedule and command helpers for the Reno step-join experiment."""

from __future__ import annotations

from dataclasses import dataclass

from reno_fairness_common import (
    build_iperf_server_command,
    parse_tc_rate_mbps,
    qdisc_commands,
)

STREAM_COUNT = 10
PHASE_LABELS = tuple(f"Phase {index}" for index in range(1, STREAM_COUNT + 1))


@dataclass(frozen=True)
class StreamInstance:
    name: str
    number: int
    start_phase: int
    server_port: int
    client_port: int

    def start_seconds(self, phase_seconds: float) -> float:
        return self.start_phase * phase_seconds

    def duration_seconds(self, phase_seconds: float) -> float:
        return (STREAM_COUNT - self.start_phase) * phase_seconds

    def end_seconds(self, phase_seconds: float) -> float:
        return STREAM_COUNT * phase_seconds


STREAMS = tuple(
    StreamInstance(
        name=f"stream_{number:02d}",
        number=number,
        start_phase=number - 1,
        server_port=5200 + number,
        client_port=6000 + number,
    )
    for number in range(1, STREAM_COUNT + 1)
)


def build_iperf_client_command(server_ip: str, stream: StreamInstance,
                               phase_seconds: float,
                               congestion: str = "reno") -> list[str]:
    """Build one unlimited, single-connection TCP client command."""
    duration = stream.duration_seconds(phase_seconds)
    if duration <= 0:
        raise ValueError("stream duration must be positive")
    if congestion not in ("reno", "cubic", "bbr"):
        raise ValueError(f"unsupported congestion controller: {congestion}")
    duration_arg = str(int(duration)) if float(duration).is_integer() else str(duration)
    return [
        "iperf3", "-c", server_ip,
        "-p", str(stream.server_port),
        "--cport", str(stream.client_port),
        "-C", congestion,
        "-t", duration_arg,
        "-i", "1",
        "-J",
    ]


def phase_boundaries(phase_seconds: float) -> list[float]:
    if phase_seconds <= 0:
        raise ValueError("phase duration must be positive")
    return [index * phase_seconds for index in range(STREAM_COUNT + 1)]


__all__ = (
    "PHASE_LABELS",
    "STREAM_COUNT",
    "STREAMS",
    "StreamInstance",
    "build_iperf_client_command",
    "build_iperf_server_command",
    "parse_tc_rate_mbps",
    "phase_boundaries",
    "qdisc_commands",
)
