#!/usr/bin/env python3
"""Pure helpers shared by the Reno/Reno fairness runner and analyzer."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Iterable, Sequence

FLOW_PORTS = {"A": 5201, "B": 5202}
FLOW_CPORTS = {"A1": 6001, "B1": 6002, "A2": 6003}
PHASE_LABELS = ("A only", "A + B", "B only", "B + A", "A only")


@dataclass(frozen=True)
class FlowInstance:
    logical_flow: str
    instance: str
    start_phase: int
    duration_phases: int
    server_port: int
    client_port: int

    def start_seconds(self, phase_seconds: float) -> float:
        return self.start_phase * phase_seconds

    def duration_seconds(self, phase_seconds: float) -> float:
        return self.duration_phases * phase_seconds

    def end_seconds(self, phase_seconds: float) -> float:
        return (self.start_phase + self.duration_phases) * phase_seconds


FLOW_INSTANCES = (
    FlowInstance("A", "A1", 0, 2, FLOW_PORTS["A"], FLOW_CPORTS["A1"]),
    FlowInstance("B", "B1", 1, 3, FLOW_PORTS["B"], FLOW_CPORTS["B1"]),
    FlowInstance("A", "A2", 3, 2, FLOW_PORTS["A"], FLOW_CPORTS["A2"]),
)


def parse_tc_rate_mbps(value: str) -> float:
    """Parse the deliberately narrow tc rate syntax accepted by this runner."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(kbit|mbit|gbit)", value.lower())
    if match is None:
        raise ValueError("rate must use tc syntax such as 20mbit")
    scale = {"kbit": 0.001, "mbit": 1.0, "gbit": 1000.0}[match.group(2)]
    return float(match.group(1)) * scale


def build_iperf_server_command(port: int) -> list[str]:
    """Build a persistent iperf3 server command for one logical flow."""
    return ["iperf3", "-s", "-p", str(port)]


def build_iperf_client_command(server_ip: str, flow: FlowInstance,
                               phase_seconds: float) -> list[str]:
    """Build one single-stream, unlimited-rate TCP Reno client invocation.

    iperf3 documents TCP/SCTP -b as unlimited when omitted, -C as the
    congestion-control selector, -J as JSON output, and --cport as the data
    stream's client port.  Fixed, distinct client ports make packet-capture
    attribution deterministic while A2 remains a fresh TCP connection.
    """
    duration = flow.duration_seconds(phase_seconds)
    if duration <= 0:
        raise ValueError("flow duration must be positive")
    duration_arg = str(int(duration)) if float(duration).is_integer() else str(duration)
    return [
        "iperf3", "-c", server_ip,
        "-p", str(flow.server_port),
        "--cport", str(flow.client_port),
        "-C", "reno",
        "-t", duration_arg,
        "-i", "1",
        "-J",
    ]


def qdisc_commands(interface: str, rate: str, fifo_limit_packets: int = 10000,
                   burst: str = "32k", cburst: str = "1600") -> list[list[str]]:
    """Return the baseline HTB + packet-limited FIFO command sequence."""
    if fifo_limit_packets <= 0:
        raise ValueError("fifo limit must be positive")
    parse_tc_rate_mbps(rate)
    return [
        ["tc", "qdisc", "del", "dev", interface, "root"],
        ["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:",
         "htb", "default", "1"],
        ["tc", "class", "add", "dev", interface, "parent", "1:",
         "classid", "1:1", "htb", "rate", rate, "ceil", rate,
         "burst", burst, "cburst", cburst],
        ["tc", "qdisc", "add", "dev", interface, "parent", "1:1",
         "handle", "10:", "pfifo", "limit", str(fifo_limit_packets)],
    ]


def phase_boundaries(phase_seconds: float) -> list[float]:
    if phase_seconds <= 0:
        raise ValueError("phase duration must be positive")
    return [index * phase_seconds for index in range(6)]


def phase_index(time_s: float, boundaries: Sequence[float]) -> int | None:
    if len(boundaries) != 6:
        raise ValueError("five-phase experiment requires six boundaries")
    if time_s < boundaries[0] or time_s >= boundaries[-1]:
        return None
    for index in range(5):
        if boundaries[index] <= time_s < boundaries[index + 1]:
            return index
    return None


def jain_fairness(values: Iterable[float]) -> float:
    values = [float(value) for value in values]
    if not values:
        raise ValueError("fairness requires at least one value")
    numerator = sum(values) ** 2
    denominator = len(values) * sum(value * value for value in values)
    return numerator / denominator if denominator else math.nan


def convergence_time(times: Sequence[float], shares: Sequence[float | None],
                     event_time: float, low: float = 0.45, high: float = 0.55,
                     consecutive: int = 3) -> float | None:
    """Return time until the share first remains inside the fairness band."""
    if len(times) != len(shares):
        raise ValueError("times/shares length mismatch")
    if not 0 <= low <= high <= 1:
        raise ValueError("invalid fairness band")
    if consecutive <= 0:
        raise ValueError("consecutive bins must be positive")
    run = 0
    run_start = None
    for time_s, share in zip(times, shares):
        if time_s < event_time or share is None or not math.isfinite(float(share)):
            run = 0
            run_start = None
            continue
        if low <= float(share) <= high:
            if run == 0:
                run_start = time_s
            run += 1
            if run >= consecutive:
                return float(run_start - event_time)
        else:
            run = 0
            run_start = None
    return None
