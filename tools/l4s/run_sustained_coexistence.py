#!/usr/bin/env python3
"""Run a sustained MoQ-vs-classic-TCP coexistence matrix."""

import argparse
import json
import os
import signal
import subprocess
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
BINARY = ROOT / "deps" / "imquic" / "src" / "imquic-sustained-moq"
MODES = ("reno", "bbr", "prague")
BACKGROUND_CONTROLLERS = ("cubic", "reno", "bbr", "bbr2")
BACKGROUND_MODULES = {"bbr": "tcp_bbr", "bbr2": "tcp_bbr2"}


def stop(process):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def qdisc(switch, rate):
    for dev in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
        subprocess.run(["tc", "qdisc", "del", "dev", dev, "root"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:",
                        "htb", "default", "1"], check=True)
        subprocess.run(["tc", "class", "add", "dev", dev, "parent", "1:",
                        "classid", "1:1", "htb", "rate", rate, "burst", "32k"],
                       check=True)
        subprocess.run(["tc", "qdisc", "add", "dev", dev, "parent", "1:1",
                        "handle", "10:", "dualpi2"], check=True)


def save_qdisc_stats(switch, case, label):
    with (case / "dualpi2-stats.txt").open("a", encoding="utf-8") as stream:
        stream.write(f"snapshot={label}\n")
        for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
            stats = detailed_tc_state(interface)
            stream.write(f"device={interface}\n{stats}")


def configure_tcp_ecn(client, server, tcp_ecn):
    for host, rule in (
        (server, ["-p", "tcp", "--dport", "5201"]),
        (client, ["-p", "tcp", "--sport", "5201"]),
    ):
        for tos in ("0x00", "0x02"):
            host.cmd("iptables -t mangle -D OUTPUT " + " ".join(rule) +
                     " -j TOS --set-tos " + tos + " 2>/dev/null || true")
    if tcp_ecn == "not-ect":
        client.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
        server.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
        server.cmd("iptables -t mangle -A OUTPUT -p tcp --dport 5201 -j TOS --set-tos 0x00")
        client.cmd("iptables -t mangle -A OUTPUT -p tcp --sport 5201 -j TOS --set-tos 0x00")
    elif tcp_ecn == "ect0":
        client.cmd("sysctl -qw net.ipv4.tcp_ecn=1")
        server.cmd("sysctl -qw net.ipv4.tcp_ecn=1")
        # Some L4S-capable kernels select ECT(1) for ECN-enabled TCP.  Keep
        # TCP's ECN negotiation enabled but rewrite only this test flow to
        # the conventional ECT(0) codepoint, in both directions.
        server.cmd("iptables -t mangle -A OUTPUT -p tcp --dport 5201 -j TOS --set-tos 0x02")
        client.cmd("iptables -t mangle -A OUTPUT -p tcp --sport 5201 -j TOS --set-tos 0x02")
    else:
        raise ValueError(f"unsupported TCP ECN mode: {tcp_ecn}")


def run_case(client, server, switch, root, mode, background_congestion,
             repetition, duration, warmup, drain):
    case = root / (
        f"{mode}-tcp-{background_congestion}-unlimited-rep-{repetition:02d}"
    )
    case.mkdir(parents=True)
    configure_tcp_ecn(client, server, "not-ect")
    qdisc(switch, "20mbit")
    save_qdisc_stats(switch, case, "before")
    metadata = {"mode": mode, "tcp_ecn": "not-ect",
                "background_congestion": background_congestion,
                "background_rate": "unlimited", "repetition": repetition,
                "duration_seconds": duration, "warmup_seconds": warmup,
                "drain_seconds": drain, "background_mbps": -1,
                "bottleneck_mbps": 20, "namespace": "imquic-l4s",
                "track": "sustained", "client_ip": client.IP(),
                "server_ip": server.IP(),
                "forward_direction": "server-to-client"}
    (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    captures, files = [], []
    publisher = subscriber = tcp_server = tcp_client = None
    try:
        for interface, name in ((f"{switch.name}-eth1", "switch-client.pcap"),
                                (f"{switch.name}-eth2", "switch-server.pcap")):
            log = (case / f"{name}.log").open("w")
            files.append(log)
            captures.append(subprocess.Popen(
                ["tcpdump", "-U", "-s", "128", "-i", interface, "-w", str(case / name)],
                stdout=log, stderr=subprocess.STDOUT))
        tcp_server_log = (case / "iperf-server.json").open("w")
        tcp_client_log = (case / "iperf-client.json").open("w")
        files.extend((tcp_server_log, tcp_client_log))
        tcp_server = client.popen(["iperf3", "-s", "-1", "-p", "5201", "--json"],
                                  stdout=tcp_server_log, stderr=subprocess.STDOUT)
        time.sleep(.3)
        tcp_started = time.time()
        tcp_client = server.popen(
            ["iperf3", "-c", client.IP(), "-p", "5201", "-t",
             str(warmup + duration + drain), "-i", "1", "-C",
             background_congestion, "--json"],
            stdout=tcp_client_log, stderr=subprocess.STDOUT)
        metadata["tcp_started_epoch"] = tcp_started
        time.sleep(warmup)
        pub_log = (case / "publisher.log").open("w")
        sub_log = (case / "subscriber.log").open("w")
        files.extend((pub_log, sub_log))
        foreground_started = time.time()
        publisher = server.popen([str(BINARY), "publisher", server.IP(), "4443",
                                  mode, str(case / "metrics.csv"), str(duration)],
                                 cwd=str(BINARY.parent), stdout=pub_log,
                                 stderr=subprocess.STDOUT)
        time.sleep(.5)
        if publisher.poll() is not None:
            raise RuntimeError(f"{case.name}: publisher exited before subscription")
        subscriber = client.popen([str(BINARY), "subscriber", server.IP(), "4443",
                                   mode, str(duration), str(case / "subscriber-result.json")],
                                  cwd=str(BINARY.parent),
                                  stdout=sub_log, stderr=subprocess.STDOUT)
        time.sleep(.5)
        if subscriber.poll() is not None:
            raise RuntimeError(f"{case.name}: subscriber exited before receiving objects")
        publisher_status = publisher.wait(timeout=duration + 30)
        subscriber_status = subscriber.wait(timeout=30)
        metadata["foreground_started_epoch"] = foreground_started
        metadata["foreground_finished_epoch"] = time.time()
        metadata["publisher_status"] = publisher_status
        metadata["subscriber_status"] = subscriber_status
        (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        if publisher_status or subscriber_status:
            raise RuntimeError(f"{case.name}: MoQ publisher/subscriber failed")
        result = case / "subscriber-result.json"
        if not result.is_file() or not json.loads(result.read_text()).get("validated"):
            raise RuntimeError(f"{case.name}: subscriber validation evidence missing")
        tcp_client.wait(timeout=30)
        tcp_server.wait(timeout=10)
        metadata["tcp_finished_epoch"] = time.time()
        (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    finally:
        for process in (subscriber, publisher, tcp_client, tcp_server, *captures):
            stop(process)
        save_qdisc_stats(switch, case, "after")
        for stream in files:
            stream.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--drain", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument(
        "--background-congestions", default=",".join(BACKGROUND_CONTROLLERS),
        help="comma-separated unlimited non-ECN TCP congestion controllers",
    )
    args = parser.parse_args()
    modes = args.modes.split(",")
    backgrounds = args.background_congestions.split(",")
    if (not modes or len(set(modes)) != len(modes)
            or any(mode not in MODES for mode in modes)):
        parser.error(f"modes must be unique members of {','.join(MODES)}")
    if (not backgrounds or len(set(backgrounds)) != len(backgrounds)
            or any(cc not in BACKGROUND_CONTROLLERS for cc in backgrounds)):
        parser.error(
            "background congestions must be unique members of "
            + ",".join(BACKGROUND_CONTROLLERS)
        )
    if args.duration <= 0 or args.repetitions <= 0:
        parser.error("duration and repetitions must be positive")
    if args.warmup < 0 or args.drain < 0:
        parser.error("warmup and drain must be non-negative")
    if os.geteuid() != 0:
        raise SystemExit("sustained coexistence must run as root")
    if not BINARY.exists():
        raise SystemExit(f"missing {BINARY}; run make build first")
    subprocess.run(["modprobe", "sch_dualpi2"], check=True)
    for controller in backgrounds:
        module = BACKGROUND_MODULES.get(controller)
        if module is not None:
            subprocess.run(["modprobe", module], check=True)
    args.output.mkdir(parents=True, exist_ok=False)
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    server = net.addHost("server", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(client, switch); net.addLink(switch, server)
    try:
        net.start()
        write_experiment_record(
            args.output,
            scenario="sustained-moq-coexistence",
            configuration={
                "duration_seconds": args.duration,
                "warmup_seconds": args.warmup,
                "drain_seconds": args.drain,
                "repetitions": args.repetitions,
                "modes": modes,
                "background_congestions": backgrounds,
                "background_transport": "unlimited non-ECN TCP iperf3",
                "background_mbps": "unlimited",
                "bottleneck": "20mbit",
                "qdisc": {
                    "interfaces": ["s1-eth1", "s1-eth2"],
                    "root": "HTB",
                    "burst": "32k",
                    "child": "DualPI2",
                    "parameter_profile": "kernel-defaults",
                    "parameter_evidence": (
                        "exact effective values retained per case in "
                        "dualpi2-stats.txt"
                    ),
                },
            },
            topology={
                "nodes": {"client": "10.0.0.1/24", "server": "10.0.0.2/24", "switch": "s1 OVSBridge"},
                "links": ["client<->s1", "s1<->server"],
            },
            observed_network=capture_mininet_state(client, server, switch),
        )
        available = server.cmd(
            "sysctl -n net.ipv4.tcp_available_congestion_control"
        ).split()
        missing = [controller for controller in backgrounds
                   if controller not in available]
        if missing:
            raise RuntimeError(
                "unavailable background congestion controllers: "
                + ", ".join(missing)
            )
        for repetition in range(1, args.repetitions + 1):
            for background_congestion in backgrounds:
                for mode in modes:
                    print(
                        f"running {mode} MoQ against unlimited "
                        f"{background_congestion} TCP "
                        f"(repetition {repetition}/{args.repetitions})",
                        flush=True,
                    )
                    run_case(
                        client, server, switch, args.output, mode,
                        background_congestion, repetition, args.duration,
                        args.warmup, args.drain,
                    )
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)
    subprocess.run(["python3", str(ROOT / "tools/l4s/analyze_sustained_coexistence.py"),
                    str(args.output)], check=True)


if __name__ == "__main__":
    main()
