#!/usr/bin/env python3
"""Run paired IMQUIC L4S-on/off benchmarks in a one-switch Mininet topology."""

import argparse
import json
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSBridge

from experiment_metadata import (
    capture_mininet_state,
    detailed_tc_state,
    write_experiment_record,
)


ROOT = Path(__file__).resolve().parents[2]
IMQUIC_ROOT = ROOT / "deps" / "imquic"
BINARY = IMQUIC_ROOT / "src" / "imquic-l4s-test"
ANALYZER = ROOT / "tools" / "l4s" / "analyze_mininet_benchmark.py"
MODE_CONTROLLERS = {
    "l4s-off": "reno",
    "l4s-ect0": "reno-ect0",
    "l4s-on": "prague",
}
MODE_LABELS = {
    "l4s-off": "Reno Not-ECT",
    "l4s-ect0": "Reno ECT(0)",
    "l4s-on": "Prague ECT(1)",
}
BACKGROUND_CONTROLLERS = ("reno", "cubic", "bbr", "bbr2")
BACKGROUND_MODULES = {"bbr": "tcp_bbr", "bbr2": "tcp_bbr2"}


def background_rate_label(rate):
    return "unlimited" if rate == -1 else f"{rate:03d}mbps"


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def configure_dualpi2(switch, bottleneck):
    for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
        subprocess.run(
            ["tc", "qdisc", "del", "dev", interface, "root"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        command(["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:",
                 "htb", "default", "1"])
        command(["tc", "class", "add", "dev", interface, "parent", "1:",
                 "classid", "1:1", "htb", "rate", bottleneck, "burst", "32k"])
        # Keep the Linux DualPI2 reference parameters as one coherent set.
        # `target` is the Classic PI2 target; the L4S step threshold is a
        # separate sch_dualpi2 parameter. Do not override either here.
        command(["tc", "qdisc", "add", "dev", interface, "parent", "1:1",
                 "handle", "10:", "dualpi2"])


def run_case(client, server, switch, output, mode, background_mbps, repetition,
             transfer_bytes, foreground_seconds, background_warmup_seconds,
             bottleneck, bottleneck_mbps, background_congestion):
    case = output / (
        f"{mode}-bg-{background_rate_label(background_mbps)}-rep-{repetition:02d}"
    )
    case.mkdir(parents=True)
    configure_dualpi2(switch, bottleneck)
    metadata = {
        "mode": mode,
        "controller": MODE_CONTROLLERS[mode],
        "background_mbps": background_mbps,
        "background_rate": background_rate_label(background_mbps),
        "background_transport": "TCP",
        "background_congestion": background_congestion,
        "background_ecn": "disabled",
        "repetition": repetition,
        "transfer_bytes": transfer_bytes,
        "foreground_seconds": foreground_seconds,
        "background_warmup_seconds": background_warmup_seconds,
        "background_seconds": (
            foreground_seconds + background_warmup_seconds + 1
        ),
        "bottleneck": bottleneck,
        "bottleneck_mbps": bottleneck_mbps,
        "client_ip": client.IP(),
        "server_ip": server.IP(),
    }
    (case / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    captures = []
    server_process = None
    background_server = None
    background_client = None
    files = []
    try:
        for interface, name in ((f"{switch.name}-eth1", "switch-client.pcap"),
                                (f"{switch.name}-eth2", "switch-server.pcap")):
            log = (case / f"tcpdump-{interface}.log").open("w", encoding="utf-8")
            files.append(log)
            captures.append(subprocess.Popen(
                [
                    "tcpdump", "-U", "-s", "128", "-i", interface,
                    "-w", str(case / name),
                ],
                stdout=log, stderr=subprocess.STDOUT,
            ))
        time.sleep(0.1)
        if any(process.poll() is not None for process in captures):
            raise RuntimeError(f"{case.name}: tcpdump failed to start")

        server_log = (case / "server.log").open("w", encoding="utf-8")
        client_log = (case / "client.log").open("w", encoding="utf-8")
        files.extend((server_log, client_log))
        controller = metadata["controller"]
        server_process = server.popen(
            [str(BINARY), "--server", server.IP(), "4443", controller,
             str(transfer_bytes), str(foreground_seconds)],
            cwd=str(IMQUIC_ROOT / "src"), stdout=server_log, stderr=subprocess.STDOUT,
        )
        time.sleep(0.5)

        if background_mbps != 0:
            iperf_server_log = (case / "iperf-server.json").open("w", encoding="utf-8")
            iperf_client_log = (case / "iperf-client.json").open("w", encoding="utf-8")
            files.extend((iperf_server_log, iperf_client_log))
            background_server = server.popen(
                ["iperf3", "-s", "-1", "-p", "5201", "--json"],
                stdout=iperf_server_log, stderr=subprocess.STDOUT,
            )
            time.sleep(0.3)
            background_command = [
                "iperf3", "-c", server.IP(), "-p", "5201", "-t",
                str(metadata["background_seconds"]),
            ]
            if background_mbps > 0:
                background_command.extend(["-b", f"{background_mbps}M"])
            background_command.extend(["-C", background_congestion, "--json"])
            background_client = client.popen(
                background_command,
                stdout=iperf_client_log, stderr=subprocess.STDOUT,
            )
            time.sleep(background_warmup_seconds)

        started = time.monotonic()
        metadata["quic_started_epoch"] = time.time()
        client_process = client.popen(
            [str(BINARY), "--client", server.IP(), "4443", str(case / "metrics.csv"),
             controller, str(transfer_bytes), str(foreground_seconds)],
            cwd=str(IMQUIC_ROOT / "src"), stdout=client_log, stderr=subprocess.STDOUT,
        )
        client_status = client_process.wait(timeout=foreground_seconds + 30)
        metadata["quic_finished_epoch"] = time.time()
        server_status = server_process.wait(timeout=30)
        metadata["wall_duration_seconds"] = time.monotonic() - started
        metadata["client_status"] = client_status
        metadata["server_status"] = server_status
        (case / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if client_status != 0 or server_status != 0:
            raise RuntimeError(f"{case.name}: IMQUIC client/server failed")
        if background_client is not None and background_client.wait(timeout=30) != 0:
            raise RuntimeError(f"{case.name}: background TCP client failed")
        if background_server is not None and background_server.wait(timeout=10) != 0:
            raise RuntimeError(f"{case.name}: background TCP server failed")
    finally:
        for process in captures:
            stop_process(process)
        stop_process(background_client)
        stop_process(background_server)
        stop_process(server_process)
        for stream in files:
            stream.close()

    with (case / "dualpi2-stats.txt").open("w", encoding="utf-8") as stream:
        for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
            stream.write(f"device={interface}\n")
            stream.write(detailed_tc_state(interface))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--background-mbps", default="0,5,10,20")
    parser.add_argument("--bottleneck", default="20mbit")
    parser.add_argument("--transfer-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--foreground-seconds", type=int, default=8)
    parser.add_argument("--background-warmup-seconds", type=int, default=2)
    parser.add_argument(
        "--background-congestion",
        choices=BACKGROUND_CONTROLLERS,
        default="bbr",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--modes", default="l4s-off,l4s-ect0,l4s-on",
        help="comma-separated benchmark modes",
    )
    parser.add_argument("--reference-summary", type=Path)
    args = parser.parse_args()
    rates = []
    for value in args.background_mbps.split(","):
        if value == "unlimited":
            rates.append(-1)
        else:
            try:
                rates.append(int(value))
            except ValueError:
                parser.error(
                    "background rates must be non-negative integers or unlimited"
                )
    modes = args.modes.split(",")
    if (not rates or len(set(rates)) != len(rates) or
            any(rate < -1 for rate in rates)):
        parser.error(
            "background rates must be unique non-negative integers or unlimited"
        )
    if args.repetitions <= 0:
        parser.error("repetitions must be positive")
    if args.foreground_seconds <= 0:
        parser.error("foreground seconds must be positive")
    if args.background_warmup_seconds < 0:
        parser.error("background warmup seconds must be non-negative")
    if not modes or len(set(modes)) != len(modes) or any(
        mode not in MODE_CONTROLLERS for mode in modes
    ):
        parser.error(f"modes must be unique members of {','.join(MODE_CONTROLLERS)}")
    if args.reference_summary is not None and not args.reference_summary.is_file():
        parser.error(f"reference summary not found: {args.reference_summary}")
    units = {"kbit": 0.001, "mbit": 1.0, "gbit": 1000.0}
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(kbit|mbit|gbit)", args.bottleneck)
    if match is None:
        parser.error("bottleneck must use tc rate syntax such as 20mbit")
    bottleneck_mbps = float(match.group(1)) * units[match.group(2)]
    maximum_path_bytes = (
        bottleneck_mbps * 1_000_000 / 8 * args.foreground_seconds
    )
    if args.transfer_bytes <= maximum_path_bytes * 1.25:
        parser.error(
            "transfer bytes must exceed 125% of the maximum bottleneck "
            "delivery during the foreground duration"
        )
    if os.geteuid() != 0:
        raise SystemExit("Mininet benchmark must run as root inside QEMU")
    for program in ("iperf3", "mn", "ovs-vsctl", "tc", "tcpdump", "tshark"):
        if shutil.which(program) is None:
            raise SystemExit(f"missing guest benchmark command: {program}")
    for executable in (BINARY, ANALYZER):
        if not executable.exists():
            raise SystemExit(f"missing benchmark dependency: {executable}")
    command(["modprobe", "sch_dualpi2"])
    command(["service", "openvswitch-switch", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    command(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    args.output.mkdir(parents=True, exist_ok=False)
    benchmark = {
        "topology": "client--s1(OVSBridge+HTB+DualPI2)--server",
        "kernel": platform.release(),
        "mininet": subprocess.run(
            ["mn", "--version"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout.strip(),
        "background_rates_mbps": [
            "unlimited" if rate == -1 else rate for rate in rates
        ],
        "repetitions": args.repetitions,
        "background_transport": (
            f"TCP iperf3 {args.background_congestion} with ECN disabled"
        ),
        "background_congestion": args.background_congestion,
        "bottleneck": args.bottleneck,
        "bottleneck_mbps": bottleneck_mbps,
        "transfer_bytes": args.transfer_bytes,
        "foreground_seconds": args.foreground_seconds,
        "background_warmup_seconds": args.background_warmup_seconds,
        "modes": {mode: MODE_LABELS[mode] for mode in modes},
    }
    (args.output / "benchmark.json").write_text(
        json.dumps(benchmark, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    server = net.addHost("server", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(client, switch)
    net.addLink(switch, server)
    try:
        net.start()
        client.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
        server.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
        client.cmd("iptables -t mangle -A OUTPUT -p tcp --dport 5201 -j TOS --set-tos 0x00")
        server.cmd("iptables -t mangle -A OUTPUT -p tcp --sport 5201 -j TOS --set-tos 0x00")
        if client.cmd(f"ping -c 1 -W 2 {server.IP()}").find("1 received") < 0:
            raise RuntimeError("Mininet client/server connectivity failed")
        module = BACKGROUND_MODULES.get(args.background_congestion)
        if module is not None:
            command(["modprobe", module])
        available = client.cmd("sysctl -n net.ipv4.tcp_available_congestion_control")
        if args.background_congestion not in available.split():
            raise RuntimeError(
                f"TCP {args.background_congestion} unavailable in guest kernel: "
                f"{available.strip()}"
            )
        write_experiment_record(
            args.output,
            scenario="l4s-mininet-coexistence",
            configuration={
                **benchmark,
                "background_seconds": (
                    args.foreground_seconds + args.background_warmup_seconds + 1
                ),
                "reference_summary": (
                    str(args.reference_summary) if args.reference_summary else None
                ),
                "background_ecn_enforcement": "tcp_ecn=0 and TOS 0x00 on port 5201",
                "qdisc": {
                    "interfaces": ["s1-eth1", "s1-eth2"],
                    "root": "HTB",
                    "rate": args.bottleneck,
                    "burst": "32k",
                    "child": "DualPI2",
                    "parameter_profile": "kernel-defaults",
                    "parameter_evidence":
                        "exact effective values are retained per case in dualpi2-stats.txt",
                },
            },
            topology={
                "nodes": {"client": "10.0.0.1/24", "server": "10.0.0.2/24", "switch": "s1 OVSBridge"},
                "links": ["client<->s1", "s1<->server"],
                "bottleneck_interfaces": ["s1-eth1", "s1-eth2"],
            },
            observed_network=capture_mininet_state(client, server, switch),
        )
        for rate in rates:
            for repetition in range(1, args.repetitions + 1):
                for mode in modes:
                    rate_description = (
                        "unlimited" if rate == -1 else f"{rate} Mbps"
                    )
                    print(
                        f"running {mode} with {rate_description} "
                        f"{args.background_congestion} TCP background "
                        f"(repetition {repetition}/{args.repetitions})",
                        flush=True,
                    )
                    run_case(
                        client, server, switch, args.output, mode, rate,
                        repetition, args.transfer_bytes,
                        args.foreground_seconds,
                        args.background_warmup_seconds,
                        args.bottleneck, bottleneck_mbps,
                        args.background_congestion,
                    )
    finally:
        net.stop()
        command(["mn", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    analyzer_command = [sys.executable, str(ANALYZER), str(args.output)]
    if args.reference_summary is not None:
        analyzer_command.extend(
            ["--reference-summary", str(args.reference_summary)]
        )
    command(analyzer_command)
    print(f"Mininet L4S benchmark: PASS ({args.output})")


if __name__ == "__main__":
    main()
