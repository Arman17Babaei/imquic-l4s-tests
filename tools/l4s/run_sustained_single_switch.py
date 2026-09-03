#!/usr/bin/env python3
"""Run one capture-free sustained MoQ flow through one DualPI2 switch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

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
FIXTURE_SOURCE = ROOT / "tests" / "sustained-moq-test.c"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stop(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def interface(left, right) -> str:
    links = left.connectionsTo(right)
    if len(links) != 1:
        raise RuntimeError(f"expected one link between {left.name} and {right.name}")
    return str(links[0][0])


def run_in_node(node, command: list[str], *, check: bool = True) -> None:
    process = node.popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()
    if check and process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, command, output=stdout, stderr=stderr
        )


def configure_delay(node, device: str, one_way_ms: float) -> None:
    run_in_node(node, ["tc", "qdisc", "del", "dev", device, "root"], check=False)
    if one_way_ms:
        run_in_node(
            node,
            [
                "tc", "qdisc", "add", "dev", device, "root", "netem",
                "delay", f"{one_way_ms:g}ms", "limit", "100000",
            ],
        )


def configure_dualpi2(device: str, args: argparse.Namespace) -> None:
    subprocess.run(
        ["tc", "qdisc", "del", "dev", device, "root"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["tc", "qdisc", "add", "dev", device, "root", "handle", "1:",
         "htb", "default", "1"],
        check=True,
    )
    subprocess.run(
        [
            "tc", "class", "add", "dev", device, "parent", "1:",
            "classid", "1:1", "htb", "rate", f"{args.bottleneck_mbps:g}mbit",
            "burst", args.burst, "cburst", args.burst,
        ],
        check=True,
    )
    subprocess.run(
        [
            "tc", "qdisc", "add", "dev", device, "parent", "1:1",
            "handle", "10:", "dualpi2", "target", args.dualpi2_target,
            "tupdate", args.dualpi2_tupdate, "step_thresh", args.dualpi2_step,
        ],
        check=True,
    )


def save_tc(case: Path, label: str, devices: dict[str, str]) -> None:
    with (case / f"tc-state-{label}.txt").open("w", encoding="utf-8") as stream:
        for name, device in devices.items():
            stream.write(f"{name}={device}\n{detailed_tc_state(device)}")


def run_case(server, client, switch_egress: str, endpoint_devices: dict[str, str],
             root: Path, args: argparse.Namespace, repetition: int) -> None:
    case = root / f"prague-sustained-rep-{repetition:02d}"
    case.mkdir()
    configure_dualpi2(switch_egress, args)
    save_tc(case, "before", {"switch_client_egress": switch_egress})
    metadata = {
        "mode": "prague",
        "repetition": repetition,
        "duration_seconds": args.duration,
        "bottleneck_mbps": args.bottleneck_mbps,
        "base_rtt_ms": args.base_rtt_ms,
        "background": "none",
        "capture": "none",
        "forward_direction": "server-to-client",
        "switch_client_egress": switch_egress,
        **endpoint_devices,
    }
    (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    publisher = subscriber = None
    files = []
    try:
        publisher_log = (case / "publisher.log").open("w")
        subscriber_log = (case / "subscriber.log").open("w")
        files.extend((publisher_log, subscriber_log))
        started = time.time()
        publisher = server.popen(
            [
                str(BINARY), "publisher", server.IP(), "4443", "prague",
                str(case / "transport-metrics.csv"), str(args.duration),
            ],
            cwd=str(BINARY.parent),
            stdout=publisher_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.5)
        if publisher.poll() is not None:
            raise RuntimeError("publisher exited before subscription")
        subscriber = client.popen(
            [
                str(BINARY), "subscriber", server.IP(), "4443", "prague",
                str(args.duration), str(case / "subscriber-result.json"),
                str(case / "arrival-timeline.csv"),
            ],
            cwd=str(BINARY.parent),
            stdout=subscriber_log,
            stderr=subprocess.STDOUT,
        )
        publisher_status = publisher.wait(timeout=args.duration + 30)
        subscriber_status = subscriber.wait(timeout=30)
        metadata.update({
            "wall_duration_seconds": time.time() - started,
            "publisher_status": publisher_status,
            "subscriber_status": subscriber_status,
        })
        (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        if publisher_status or subscriber_status:
            raise RuntimeError("sustained publisher/subscriber failed")
        result = json.loads((case / "subscriber-result.json").read_text())
        if not result.get("validated"):
            raise RuntimeError("subscriber validation failed")
        timeline = case / "arrival-timeline.csv"
        timeline_rows = 0
        if timeline.is_file():
            with timeline.open() as stream:
                timeline_rows = sum(1 for _ in stream) - 1
        if timeline_rows <= 0:
            raise RuntimeError("receiver payload-arrival timeline is missing or empty")
    finally:
        stop(subscriber)
        stop(publisher)
        save_tc(case, "after", {"switch_client_egress": switch_egress})
        for stream in files:
            stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--bottleneck-mbps", type=float, default=50)
    parser.add_argument("--base-rtt-ms", type=float, default=20)
    parser.add_argument("--burst", default="32k")
    parser.add_argument("--dualpi2-target", default="15ms")
    parser.add_argument("--dualpi2-tupdate", default="16ms")
    parser.add_argument("--dualpi2-step", default="1ms")
    args = parser.parse_args()
    if args.duration <= 0 or args.repetitions <= 0 or args.bottleneck_mbps <= 0:
        parser.error("duration, repetitions, and bottleneck Mbps must be positive")
    if args.base_rtt_ms < 0:
        parser.error("base RTT must be non-negative")
    if os.geteuid() != 0:
        raise SystemExit("single-switch sustained test must run as root")
    if not BINARY.exists():
        raise SystemExit(f"missing {BINARY}; run make build first")

    subprocess.run(["modprobe", "sch_dualpi2"], check=True)
    args.output.mkdir(parents=True, exist_ok=False)
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    server = net.addHost("server", ip="10.0.0.1/24")
    client = net.addHost("client", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(server, switch)
    net.addLink(switch, client)
    try:
        net.start()
        server_egress = interface(server, switch)
        client_egress = interface(client, switch)
        switch_client_egress = interface(switch, client)
        one_way_ms = args.base_rtt_ms / 2
        configure_delay(server, server_egress, one_way_ms)
        configure_delay(client, client_egress, one_way_ms)
        configuration = {
            "duration_seconds": args.duration,
            "repetitions": args.repetitions,
            "mode": "prague",
            "background": "none",
            "capture": "none",
            "receiver_timeline": "per-object payload arrival",
            "fixture_source": str(FIXTURE_SOURCE),
            "fixture_source_sha256": sha256_file(FIXTURE_SOURCE),
            "bottleneck_mbps": args.bottleneck_mbps,
            "base_rtt_ms": args.base_rtt_ms,
            "qdisc": {
                "interface": switch_client_egress,
                "root": "HTB",
                "burst": args.burst,
                "child": "DualPI2",
                "target": args.dualpi2_target,
                "tupdate": args.dualpi2_tupdate,
                "step_thresh": args.dualpi2_step,
            },
        }
        write_experiment_record(
            args.output,
            scenario="sustained-moq-single-client-switch",
            configuration=configuration,
            topology={
                "forward": "server -> s1(DualPI2 on client egress) -> client",
                "links": ["server<->s1", "s1<->client"],
                "base_rtt": "base_rtt_ms/2 on each endpoint egress",
                "interfaces": {
                    "server_egress": server_egress,
                    "switch_client_egress": switch_client_egress,
                    "client_egress": client_egress,
                },
            },
            observed_network=capture_mininet_state(client, server, switch),
        )
        endpoint_devices = {
            "server_egress": server_egress,
            "client_egress": client_egress,
        }
        for repetition in range(1, args.repetitions + 1):
            print(
                f"running sustained Prague repetition {repetition}/{args.repetitions}",
                flush=True,
            )
            run_case(
                server, client, switch_client_egress, endpoint_devices,
                args.output, args, repetition,
            )
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)


if __name__ == "__main__":
    main()
