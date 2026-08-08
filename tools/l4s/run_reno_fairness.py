#!/usr/bin/env python3
"""Run the five-phase Reno/Reno fairness diagnostic in Mininet.

The data-plane schedule is deliberately symmetric:
A alone -> A+B -> B alone -> B+A -> A alone.
A1 and A2 are separate iperf3 invocations and therefore fresh TCP connections.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from reno_fairness_common import (
    FLOW_INSTANCES,
    FLOW_PORTS,
    FlowInstance,
    build_iperf_client_command,
    build_iperf_server_command,
    parse_tc_rate_mbps,
    qdisc_commands,
)

ROOT = Path(__file__).resolve().parents[2]
ANALYZER = ROOT / "tools" / "l4s" / "analyze_reno_fairness.py"


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def stop_process(process) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def configure_fifo_bottleneck(interface: str, rate: str,
                              fifo_limit_packets: int) -> None:
    for index, args in enumerate(qdisc_commands(interface, rate, fifo_limit_packets)):
        subprocess.run(
            args,
            check=index != 0,
            text=True,
            stdout=subprocess.DEVNULL if index == 0 else None,
            stderr=subprocess.DEVNULL if index == 0 else None,
        )


def save_qdisc_stats(interface: str, path: Path) -> None:
    result = command(["tc", "-s", "qdisc", "show", "dev", interface],
                     capture_output=True)
    path.write_text(result.stdout, encoding="utf-8")


def configure_not_ect(client, server) -> None:
    """Disable TCP ECN and defensively clear the ECN codepoint for test traffic."""
    client.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
    server.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
    client.cmd(
        f"iptables -t mangle -A OUTPUT -p tcp -d {server.IP()} "
        "-j TOS --set-tos 0x00"
    )
    server.cmd(
        f"iptables -t mangle -A OUTPUT -p tcp -d {client.IP()} "
        "-j TOS --set-tos 0x00"
    )


def remove_not_ect_rules(client, server) -> None:
    client.cmd(
        f"iptables -t mangle -D OUTPUT -p tcp -d {server.IP()} "
        "-j TOS --set-tos 0x00 2>/dev/null || true"
    )
    server.cmd(
        f"iptables -t mangle -D OUTPUT -p tcp -d {client.IP()} "
        "-j TOS --set-tos 0x00 2>/dev/null || true"
    )


def execute_flow_schedule(
    phase_seconds: float,
    launch: Callable[[FlowInstance], object],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
    origin: float | None = None,
) -> tuple[list[dict], dict[str, object]]:
    """Launch/poll the three iperf clients against one monotonic clock.

    This function is Mininet-independent so the exact timing/state machine can
    be tested with fake clocks and fake processes.
    """
    if phase_seconds <= 0:
        raise ValueError("phase duration must be positive")
    if poll_interval <= 0:
        raise ValueError("poll interval must be positive")
    origin = clock() if origin is None else origin
    pending = list(FLOW_INSTANCES)
    active: dict[str, tuple[FlowInstance, object]] = {}
    processes: dict[str, object] = {}
    events: list[dict] = []

    while pending or active:
        now = clock()
        relative = now - origin
        while pending and relative + 1e-9 >= pending[0].start_seconds(phase_seconds):
            flow = pending.pop(0)
            process = launch(flow)
            active[flow.instance] = (flow, process)
            processes[flow.instance] = process
            events.append({
                "time_s": relative,
                "scheduled_time_s": flow.start_seconds(phase_seconds),
                "flow": flow.logical_flow,
                "instance": flow.instance,
                "event": "start",
                "status": "",
            })
            now = clock()
            relative = now - origin

        for instance, (flow, process) in list(active.items()):
            status = process.poll()
            if status is not None:
                events.append({
                    "time_s": clock() - origin,
                    "scheduled_time_s": flow.end_seconds(phase_seconds),
                    "flow": flow.logical_flow,
                    "instance": flow.instance,
                    "event": "end",
                    "status": status,
                })
                del active[instance]

        if not pending and not active:
            break
        now_relative = clock() - origin
        delay = poll_interval
        if pending:
            delay = min(delay, max(0.0, pending[0].start_seconds(phase_seconds) - now_relative))
        sleeper(delay if delay > 0 else min(poll_interval, 0.001))

    return events, processes


def write_events(path: Path, events: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("time_s", "scheduled_time_s", "flow", "instance", "event", "status"),
        )
        writer.writeheader()
        writer.writerows(events)


def run_repetition(client, server, switch, root: Path, repetition: int,
                   phase_seconds: float, bottleneck: str,
                   fifo_limit_packets: int) -> None:
    case = root / f"rep_{repetition:03d}"
    case.mkdir(parents=True)
    bottleneck_interface = f"{switch.name}-eth2"
    configure_not_ect(client, server)
    configure_fifo_bottleneck(bottleneck_interface, bottleneck, fifo_limit_packets)
    save_qdisc_stats(bottleneck_interface, case / "qdisc_before.txt")

    metadata = {
        "repetition": repetition,
        "phase_seconds": phase_seconds,
        "bottleneck": bottleneck,
        "bottleneck_mbps": parse_tc_rate_mbps(bottleneck),
        "fifo_limit_packets": fifo_limit_packets,
        "bottleneck_interface": bottleneck_interface,
        "client_ip": client.IP(),
        "server_ip": server.IP(),
        "tcp_congestion": "reno",
        "tcp_ecn": "not-ect",
        "topology": "client--s1(OVSBridge)--server; HTB+pfifo on s1-eth2",
        "flows": [
            {
                "flow": flow.logical_flow,
                "instance": flow.instance,
                "server_port": flow.server_port,
                "client_port": flow.client_port,
                "scheduled_start_s": flow.start_seconds(phase_seconds),
                "scheduled_end_s": flow.end_seconds(phase_seconds),
            }
            for flow in FLOW_INSTANCES
        ],
    }

    files = []
    capture = None
    servers = []
    try:
        capture_log = (case / "tcpdump.log").open("w", encoding="utf-8")
        files.append(capture_log)
        capture = subprocess.Popen(
            ["tcpdump", "-U", "-s", "128", "-i", bottleneck_interface,
             "-w", str(case / "bottleneck.pcap")],
            stdout=capture_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.1)
        if capture.poll() is not None:
            raise RuntimeError(f"{case.name}: tcpdump failed to start")

        for flow_name, port in FLOW_PORTS.items():
            stream = (case / f"iperf_server_{flow_name}.log").open("w", encoding="utf-8")
            files.append(stream)
            process = server.popen(
                build_iperf_server_command(port),
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            servers.append(process)
        time.sleep(0.3)
        if any(process.poll() is not None for process in servers):
            raise RuntimeError(f"{case.name}: iperf3 server failed to start")

        client_streams = {}

        def launch(flow: FlowInstance):
            stream = (case / f"iperf_{flow.instance}.json").open("w", encoding="utf-8")
            files.append(stream)
            client_streams[flow.instance] = stream
            return client.popen(
                build_iperf_client_command(server.IP(), flow, phase_seconds),
                stdout=stream,
                stderr=subprocess.STDOUT,
            )

        experiment_epoch = time.time()
        experiment_monotonic = time.monotonic()
        metadata["experiment_started_epoch"] = experiment_epoch
        events, processes = execute_flow_schedule(
            phase_seconds,
            launch,
            origin=experiment_monotonic,
        )
        metadata["experiment_finished_epoch"] = time.time()
        write_events(case / "events.csv", events)
        statuses = {instance: process.poll() for instance, process in processes.items()}
        metadata["client_statuses"] = statuses
        if any(status != 0 for status in statuses.values()):
            raise RuntimeError(f"{case.name}: iperf3 client failed: {statuses}")
    finally:
        stop_process(capture)
        for process in servers:
            stop_process(process)
        save_qdisc_stats(bottleneck_interface, case / "qdisc_after.txt")
        remove_not_ect_rules(client, server)
        for stream in files:
            stream.close()
        (case / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase-seconds", type=float, default=15.0)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--bottleneck", default="20mbit")
    parser.add_argument("--fifo-limit-packets", type=int, default=10000)
    parser.add_argument("--skip-analysis", action="store_true")
    args = parser.parse_args()

    if args.phase_seconds <= 0:
        parser.error("phase-seconds must be positive")
    if args.repetitions <= 0:
        parser.error("repetitions must be positive")
    if args.fifo_limit_packets <= 0:
        parser.error("fifo-limit-packets must be positive")
    try:
        bottleneck_mbps = parse_tc_rate_mbps(args.bottleneck)
    except ValueError as error:
        parser.error(str(error))
    if os.geteuid() != 0:
        raise SystemExit("Reno fairness experiment must run as root")
    for program in ("iperf3", "mn", "ovs-vsctl", "tc", "tcpdump", "tshark", "iptables"):
        if shutil.which(program) is None:
            raise SystemExit(f"missing experiment command: {program}")
    if not ANALYZER.exists() and not args.skip_analysis:
        raise SystemExit(f"missing analyzer: {ANALYZER}")

    # Lazy imports keep helper/scheduler tests runnable on systems without Mininet.
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge

    command(["service", "openvswitch-switch", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    command(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    args.output.mkdir(parents=True, exist_ok=False)
    experiment = {
        "track": "reno-fairness",
        "repetitions": args.repetitions,
        "phase_seconds": args.phase_seconds,
        "bottleneck": args.bottleneck,
        "bottleneck_mbps": bottleneck_mbps,
        "fifo_limit_packets": args.fifo_limit_packets,
        "kernel": platform.release(),
        "mininet": subprocess.run(
            ["mn", "--version"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout.strip(),
        "iperf3": subprocess.run(
            ["iperf3", "--version"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout.splitlines()[0].strip(),
        "schedule": "A -> A+B -> B -> B+A -> A",
    }
    (args.output / "experiment.json").write_text(
        json.dumps(experiment, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    server = net.addHost("server", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(client, switch)
    net.addLink(switch, server)
    try:
        net.start()
        if client.cmd(f"ping -c 1 -W 2 {server.IP()}").find("1 received") < 0:
            raise RuntimeError("Mininet client/server connectivity failed")
        available = client.cmd("sysctl -n net.ipv4.tcp_available_congestion_control")
        if "reno" not in available.split():
            raise RuntimeError(f"TCP Reno unavailable in guest kernel: {available.strip()}")
        for repetition in range(1, args.repetitions + 1):
            print(f"running Reno/Reno fairness repetition {repetition}/{args.repetitions}")
            run_repetition(
                client,
                server,
                switch,
                args.output,
                repetition,
                args.phase_seconds,
                args.bottleneck,
                args.fifo_limit_packets,
            )
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)

    if not args.skip_analysis:
        command(["python3", str(ANALYZER), str(args.output)])


if __name__ == "__main__":
    main()
