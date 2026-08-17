#!/usr/bin/env python3
"""Run the ten-phase independent Reno stream step-join experiment."""

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

from experiment_metadata import (
    capture_mininet_state,
    detailed_tc_state,
    write_experiment_record,
)

from reno_step_join_common import (
    STREAM_COUNT,
    STREAMS,
    StreamInstance,
    build_iperf_client_command,
    build_iperf_server_command,
    parse_tc_rate_mbps,
    phase_boundaries,
    qdisc_commands,
)

ROOT = Path(__file__).resolve().parents[2]
ANALYZER = ROOT / "tools" / "l4s" / "analyze_reno_step_join.py"


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
    path.write_text(detailed_tc_state(interface), encoding="utf-8")


def configure_not_ect(client, server) -> None:
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


def execute_stream_schedule(
    phase_seconds: float,
    launch: Callable[[StreamInstance], object],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
    origin: float | None = None,
) -> tuple[list[dict], dict[str, object]]:
    """Launch ten clients on absolute phase boundaries and poll to completion."""
    if phase_seconds <= 0:
        raise ValueError("phase duration must be positive")
    if poll_interval <= 0:
        raise ValueError("poll interval must be positive")
    origin = clock() if origin is None else origin
    pending = list(STREAMS)
    active: dict[str, tuple[StreamInstance, object]] = {}
    processes: dict[str, object] = {}
    events: list[dict] = []

    while pending or active:
        relative = clock() - origin
        while pending and relative + 1e-9 >= pending[0].start_seconds(phase_seconds):
            stream = pending.pop(0)
            process = launch(stream)
            active[stream.name] = (stream, process)
            processes[stream.name] = process
            events.append({
                "time_s": relative,
                "scheduled_time_s": stream.start_seconds(phase_seconds),
                "stream": stream.name,
                "event": "start",
                "status": "",
            })
            relative = clock() - origin

        for name, (stream, process) in list(active.items()):
            status = process.poll()
            if status is not None:
                events.append({
                    "time_s": clock() - origin,
                    "scheduled_time_s": stream.end_seconds(phase_seconds),
                    "stream": name,
                    "event": "end",
                    "status": status,
                })
                del active[name]

        if not pending and not active:
            break
        now_relative = clock() - origin
        delay = poll_interval
        if pending:
            delay = min(delay, max(
                0.0, pending[0].start_seconds(phase_seconds) - now_relative
            ))
        sleeper(delay if delay > 0 else min(poll_interval, 0.001))

    return events, processes


def validate_schedule_events(events: list[dict], phase_seconds: float,
                             early_tolerance: float = 0.1) -> None:
    """Reject missing, failed, or prematurely completed stream processes."""
    endpoint = STREAM_COUNT * phase_seconds
    starts = {row["stream"]: row for row in events if row["event"] == "start"}
    ends = {row["stream"]: row for row in events if row["event"] == "end"}
    expected = {stream.name for stream in STREAMS}
    if (set(starts) != expected or set(ends) != expected
            or len(starts) + len(ends) != len(events)):
        raise RuntimeError("stream schedule did not record exactly one start and end per stream")
    failed = {name: int(row["status"]) for name, row in ends.items()
              if int(row["status"]) != 0}
    if failed:
        raise RuntimeError(f"iperf3 clients failed: {failed}")
    early = {name: float(row["time_s"]) for name, row in ends.items()
             if float(row["time_s"]) + early_tolerance < endpoint}
    if early:
        raise RuntimeError(f"streams ended before the experiment endpoint: {early}")


def write_events(path: Path, events: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=("time_s", "scheduled_time_s", "stream", "event", "status"),
        )
        writer.writeheader()
        writer.writerows(events)


def run_repetition(client, server, switch, root: Path, repetition: int,
                   phase_seconds: float, bottleneck: str,
                   fifo_limit_packets: int, congestion: str) -> None:
    case = root / f"rep_{repetition:03d}"
    case.mkdir(parents=True)
    bottleneck_interface = f"{switch.name}-eth2"
    configure_not_ect(client, server)
    configure_fifo_bottleneck(bottleneck_interface, bottleneck, fifo_limit_packets)
    save_qdisc_stats(bottleneck_interface, case / "qdisc_before.txt")

    metadata = {
        "repetition": repetition,
        "phase_seconds": phase_seconds,
        "phase_count": STREAM_COUNT,
        "experiment_duration_s": STREAM_COUNT * phase_seconds,
        "bottleneck": bottleneck,
        "bottleneck_mbps": parse_tc_rate_mbps(bottleneck),
        "fifo_limit_packets": fifo_limit_packets,
        "bottleneck_interface": bottleneck_interface,
        "client_ip": client.IP(),
        "server_ip": server.IP(),
        "tcp_congestion": congestion,
        "tcp_ecn": "not-ect",
        "topology": "client--s1(OVSBridge)--server; HTB+pfifo on s1-eth2",
        "streams": [
            {
                "stream": stream.name,
                "number": stream.number,
                "server_port": stream.server_port,
                "client_port": stream.client_port,
                "scheduled_start_s": stream.start_seconds(phase_seconds),
                "scheduled_end_s": stream.end_seconds(phase_seconds),
            }
            for stream in STREAMS
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

        for stream in STREAMS:
            server_log = (case / f"iperf_server_{stream.name}.log").open(
                "w", encoding="utf-8"
            )
            files.append(server_log)
            servers.append(server.popen(
                build_iperf_server_command(stream.server_port),
                stdout=server_log,
                stderr=subprocess.STDOUT,
            ))
        time.sleep(0.3)
        if any(process.poll() is not None for process in servers):
            raise RuntimeError(f"{case.name}: iperf3 server failed to start")

        def launch(stream: StreamInstance):
            output = (case / f"iperf_{stream.name}.json").open("w", encoding="utf-8")
            files.append(output)
            return client.popen(
                build_iperf_client_command(
                    server.IP(), stream, phase_seconds, congestion
                ),
                stdout=output,
                stderr=subprocess.STDOUT,
            )

        metadata["experiment_started_epoch"] = time.time()
        experiment_monotonic = time.monotonic()
        events, processes = execute_stream_schedule(
            phase_seconds,
            launch,
            origin=experiment_monotonic,
        )
        metadata["experiment_finished_epoch"] = time.time()
        write_events(case / "events.csv", events)
        metadata["client_statuses"] = {
            name: process.poll() for name, process in processes.items()
        }
        validate_schedule_events(events, phase_seconds)
    finally:
        stop_process(capture)
        for process in servers:
            stop_process(process)
        save_qdisc_stats(bottleneck_interface, case / "qdisc_after.txt")
        remove_not_ect_rules(client, server)
        for output in files:
            output.close()
        (case / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase-seconds", type=float, default=5.0)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--bottleneck", default="20mbit")
    parser.add_argument("--fifo-limit-packets", type=int, default=10000)
    parser.add_argument(
        "--congestion", choices=("reno", "cubic", "bbr"), default="reno"
    )
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
        raise SystemExit("Reno step-join experiment must run as root")
    for program in ("iperf3", "mn", "ovs-vsctl", "tc", "tcpdump", "tshark", "iptables"):
        if shutil.which(program) is None:
            raise SystemExit(f"missing experiment command: {program}")
    if not ANALYZER.exists() and not args.skip_analysis:
        raise SystemExit(f"missing analyzer: {ANALYZER}")

    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge

    command(["service", "openvswitch-switch", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    command(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    args.output.mkdir(parents=True, exist_ok=False)
    experiment = {
        "track": "reno-step-join",
        "repetitions": args.repetitions,
        "phase_count": STREAM_COUNT,
        "phase_seconds": args.phase_seconds,
        "experiment_duration_s": phase_boundaries(args.phase_seconds)[-1],
        "bottleneck": args.bottleneck,
        "bottleneck_mbps": bottleneck_mbps,
        "fifo_limit_packets": args.fifo_limit_packets,
        "tcp_congestion": args.congestion,
        "tcp_ecn": "not-ect",
        "topology": "client--s1(OVSBridge)--server; HTB+pfifo on s1-eth2",
        "kernel": platform.release(),
        "mininet": subprocess.run(
            ["mn", "--version"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout.strip(),
        "iperf3": subprocess.run(
            ["iperf3", "--version"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout.splitlines()[0].strip(),
        "schedule": "stream i starts at (i-1)*phase_seconds and runs to the endpoint",
        "streams": [
            {
                "stream": stream.name,
                "number": stream.number,
                "server_port": stream.server_port,
                "client_port": stream.client_port,
                "scheduled_start_s": stream.start_seconds(args.phase_seconds),
                "scheduled_end_s": stream.end_seconds(args.phase_seconds),
            }
            for stream in STREAMS
        ],
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
        if args.congestion == "bbr":
            modprobe = shutil.which("modprobe")
            if modprobe is not None:
                subprocess.run(
                    [modprobe, "tcp_bbr"], check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        available = client.cmd("sysctl -n net.ipv4.tcp_available_congestion_control")
        if args.congestion not in available.split():
            raise RuntimeError(
                f"TCP {args.congestion} unavailable in guest kernel: {available.strip()}"
            )
        write_experiment_record(
            args.output,
            scenario="tcp-step-join",
            configuration={
                **experiment,
                "qdisc_commands": qdisc_commands(
                    "s1-eth2", args.bottleneck, args.fifo_limit_packets
                ),
            },
            topology={
                "nodes": {"client": "10.0.0.1/24", "server": "10.0.0.2/24", "switch": "s1 OVSBridge"},
                "links": ["client<->s1", "s1<->server"],
                "bottleneck_interface": "s1-eth2",
                "schedule": experiment["schedule"],
            },
            observed_network=capture_mininet_state(client, server, switch),
        )
        for repetition in range(1, args.repetitions + 1):
            print(f"running Reno step-join repetition {repetition}/{args.repetitions}")
            run_repetition(
                client, server, switch, args.output, repetition,
                args.phase_seconds, args.bottleneck, args.fifo_limit_packets,
                args.congestion,
            )
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)

    if not args.skip_analysis:
        command(["python3", str(ANALYZER), str(args.output)])


if __name__ == "__main__":
    main()
