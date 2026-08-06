#!/usr/bin/env python3
"""Run the 60-second MoQ-vs-ECN-disabled-TCP coexistence matrix."""

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

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "deps" / "imquic" / "src" / "imquic-sustained-moq"
MODES = ("l4s-off", "l4s-ect0", "l4s-on")


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
                        "handle", "10:", "dualpi2", "target", "1ms",
                 "tupdate", "1ms"], check=True)


def save_qdisc_stats(switch, case, label):
    with (case / "dualpi2-stats.txt").open("a", encoding="utf-8") as stream:
        stream.write(f"snapshot={label}\n")
        for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
            stats = subprocess.run(
                ["tc", "-s", "qdisc", "show", "dev", interface],
                check=True, text=True, capture_output=True).stdout
            stream.write(f"device={interface}\n{stats}")


def run_case(client, server, switch, root, mode, repetition, duration, warmup, drain):
    case = root / f"{mode}-rep-{repetition:02d}"
    case.mkdir(parents=True)
    qdisc(switch, "20mbit")
    save_qdisc_stats(switch, case, "before")
    metadata = {"mode": mode, "repetition": repetition,
                "duration_seconds": duration, "warmup_seconds": warmup,
                "drain_seconds": drain, "background_mbps": 10,
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
             str(warmup + duration + drain), "-i", "1", "-b", "10M",
             "--json"], stdout=tcp_client_log, stderr=subprocess.STDOUT)
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
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--drain", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--modes", default=",".join(MODES))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("sustained coexistence must run as root")
    if not BINARY.exists():
        raise SystemExit(f"missing {BINARY}; run make build first")
    args.output.mkdir(parents=True, exist_ok=False)
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    server = net.addHost("server", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(client, switch); net.addLink(switch, server)
    try:
        net.start()
        client.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
        server.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
        server.cmd("iptables -t mangle -A OUTPUT -p tcp --dport 5201 -j TOS --set-tos 0x00")
        client.cmd("iptables -t mangle -A OUTPUT -p tcp --sport 5201 -j TOS --set-tos 0x00")
        for repetition in range(1, args.repetitions + 1):
            for mode in args.modes.split(","):
                run_case(client, server, switch, args.output, mode, repetition,
                         args.duration, args.warmup, args.drain)
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)
    subprocess.run(["python3", str(ROOT / "tools/l4s/analyze_sustained_coexistence.py"),
                    str(args.output)], check=True)


if __name__ == "__main__":
    main()
