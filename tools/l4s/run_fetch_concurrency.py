#!/usr/bin/env python3
"""Measure one IMQUIC connection carrying many concurrent MoQ FETCH requests."""

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

from experiment_metadata import capture_mininet_state, detailed_tc_state, write_experiment_record


ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "deps" / "imquic" / "src" / "imquic-fetch-concurrency"
FIXTURE = ROOT / "tests" / "fetch-concurrency-test.c"


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


def node_command(node, command: list[str], *, check: bool = True) -> None:
    completed = node.popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = completed.communicate()
    if check and completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command, stdout, stderr)


def configure_delay(node, device: str, one_way_ms: float) -> None:
    node_command(node, ["tc", "qdisc", "del", "dev", device, "root"], check=False)
    if one_way_ms:
        node_command(node, [
            "tc", "qdisc", "add", "dev", device, "root", "netem",
            "delay", f"{one_way_ms:g}ms", "limit", "100000",
        ])


def configure_bottleneck(device: str, args: argparse.Namespace) -> None:
    subprocess.run(
        ["tc", "qdisc", "del", "dev", device, "root"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run([
        "tc", "qdisc", "add", "dev", device, "root", "handle", "1:",
        "htb", "default", "1",
    ], check=True)
    subprocess.run([
        "tc", "class", "add", "dev", device, "parent", "1:", "classid", "1:1",
        "htb", "rate", f"{args.bottleneck_mbps:g}mbit", "burst", args.burst,
        "cburst", args.burst,
    ], check=True)
    subprocess.run([
        "tc", "qdisc", "add", "dev", device, "parent", "1:1", "handle", "10:",
        "dualpi2", "target", args.dualpi2_target, "tupdate", args.dualpi2_tupdate,
        "step_thresh", args.dualpi2_step,
    ], check=True)


def save_tc(case: Path, label: str, devices: dict[str, str]) -> None:
    with (case / f"tc-state-{label}.txt").open("w", encoding="utf-8") as stream:
        for name, device in devices.items():
            stream.write(f"[{name}] {device}\n{detailed_tc_state(device)}")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_case(server, client, devices: dict[str, str], output: Path,
             count: int, repetition: int, args: argparse.Namespace) -> dict[str, object]:
    case = output / f"n-{count:06d}-rep-{repetition:02d}"
    case.mkdir()
    for device in devices.values():
        configure_bottleneck(device, args)
    save_tc(case, "before", devices)
    metadata: dict[str, object] = {
        "n": count,
        "repetition": repetition,
        "mode": "prague",
        "ecn": "ECT(1)",
        "total_bytes": args.total_bytes,
        "bytes_per_fetch_floor": args.total_bytes // count,
        "bytes_per_fetch_remainder": args.total_bytes % count,
        "timeout_seconds": args.timeout,
        "bottleneck_mbps": args.bottleneck_mbps,
        "base_rtt_ms": args.base_rtt_ms,
        "priority_mapping": "N=1:128; otherwise round(index*255/(N-1))",
        "status": "running",
    }
    write_json(case / "metadata.json", metadata)
    server_process = client_process = None
    logs = []
    started = time.monotonic()
    try:
        server_log = (case / "server.log").open("w", encoding="utf-8")
        client_log = (case / "client.log").open("w", encoding="utf-8")
        logs.extend((server_log, client_log))
        server_process = server.popen([
            str(BINARY), "server", server.IP(), "4443", "prague", str(count),
            str(args.total_bytes), str(args.timeout), str(case / "transport-metrics.csv"),
            str(case / "server-result.json"),
        ], cwd=str(BINARY.parent), stdout=server_log, stderr=subprocess.STDOUT)
        time.sleep(0.5)
        if server_process.poll() is not None:
            raise RuntimeError("server exited before the client started")
        client_process = client.popen([
            str(BINARY), "client", server.IP(), "4443", "prague", str(count),
            str(args.total_bytes), str(args.timeout), str(case / "arrival-timeline.csv"),
            str(case / "client-result.json"),
        ], cwd=str(BINARY.parent), stdout=client_log, stderr=subprocess.STDOUT)
        try:
            client_status = client_process.wait(timeout=args.timeout + 8)
            externally_killed = False
        except subprocess.TimeoutExpired:
            externally_killed = True
            stop(client_process)
            client_status = client_process.returncode
        stop(server_process)
        server_status = server_process.returncode
        metadata.update({
            "wall_duration_seconds": time.monotonic() - started,
            "client_status": client_status,
            "server_status": server_status,
            "externally_killed": externally_killed,
            "status": "complete" if client_status == 0 and not externally_killed else "failed",
        })
    except Exception as error:
        metadata.update({
            "wall_duration_seconds": time.monotonic() - started,
            "status": "infrastructure-error",
            "error": str(error),
        })
    finally:
        stop(client_process)
        stop(server_process)
        save_tc(case, "after", devices)
        for stream in logs:
            stream.close()
        if (case / "client-result.json").is_file():
            try:
                metadata["client_result"] = json.loads(
                    (case / "client-result.json").read_text(encoding="utf-8")
                )
            except json.JSONDecodeError as error:
                metadata["client_result_error"] = str(error)
        if (case / "server-result.json").is_file():
            try:
                metadata["server_result"] = json.loads(
                    (case / "server-result.json").read_text(encoding="utf-8")
                )
            except json.JSONDecodeError as error:
                metadata["server_result_error"] = str(error)
        write_json(case / "metadata.json", metadata)
    return metadata


def parse_counts(value: str) -> list[int]:
    try:
        counts = [int(item) for item in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError("counts must be comma-separated integers") from error
    if not counts or any(count <= 0 or count > 1_000_000 for count in counts):
        raise argparse.ArgumentTypeError("counts must be between 1 and 1,000,000")
    if len(counts) != len(set(counts)):
        raise argparse.ArgumentTypeError("counts must not contain duplicates")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--counts", type=parse_counts, default=parse_counts("1,10,100,1000,10000,100000"))
    parser.add_argument("--total-bytes", type=int, default=100_000_000)
    parser.add_argument("--timeout", type=int, default=80)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--bottleneck-mbps", type=float, default=20)
    parser.add_argument("--base-rtt-ms", type=float, default=20)
    parser.add_argument("--burst", default="32k")
    parser.add_argument("--dualpi2-target", default="15ms")
    parser.add_argument("--dualpi2-tupdate", default="16ms")
    parser.add_argument("--dualpi2-step", default="1ms")
    args = parser.parse_args()
    if args.total_bytes < max(args.counts):
        parser.error("total bytes must provide at least one byte per FETCH")
    if args.timeout <= 0 or args.repetitions <= 0 or args.bottleneck_mbps <= 0:
        parser.error("timeout, repetitions, and bottleneck Mbps must be positive")
    if args.base_rtt_ms < 0:
        parser.error("base RTT must be non-negative")
    if os.geteuid() != 0:
        raise SystemExit("FETCH concurrency test must run as root")
    if not BINARY.is_file():
        raise SystemExit(f"missing {BINARY}; build the FETCH concurrency fixture first")

    subprocess.run(["modprobe", "sch_dualpi2"], check=True)
    args.output.mkdir(parents=True, exist_ok=False)
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    server = net.addHost("server", ip="10.0.0.1/24")
    client = net.addHost("client", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(server, switch)
    net.addLink(switch, client)
    summaries: list[dict[str, object]] = []
    try:
        net.start()
        server_device = interface(server, switch)
        client_device = interface(client, switch)
        switch_server = interface(switch, server)
        switch_client = interface(switch, client)
        configure_delay(server, server_device, args.base_rtt_ms / 2)
        configure_delay(client, client_device, args.base_rtt_ms / 2)
        bottlenecks = {
            "server_to_client": switch_client,
            "client_to_server": switch_server,
        }
        for device in bottlenecks.values():
            configure_bottleneck(device, args)
        write_experiment_record(
            args.output,
            scenario="imquic-moq-fetch-concurrency",
            configuration={
                "hypothesis": "There is an N beyond which one IMQUIC connection cannot open and complete all concurrent FETCH requests within 80 seconds.",
                "control": "N=1",
                "independent_variable": "concurrent FETCH count N",
                "dependent_variables": ["application goodput", "QUIC cwnd", "smoothed RTT", "finish time", "request issue and all-open times"],
                "inconclusive_if": ["QEMU or Mininet infrastructure fails", "transport samples or result records are missing"],
                "counts": args.counts,
                "total_bytes": args.total_bytes,
                "repetitions": args.repetitions,
                "timeout_seconds": args.timeout,
                "ideal_transfer_seconds": args.total_bytes * 8 / (args.bottleneck_mbps * 1_000_000),
                "mode": "Prague with ECT(1)",
                "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                "qdisc": {"root": "HTB", "child": "DualPI2", "rate_mbps": args.bottleneck_mbps, "burst": args.burst, "target": args.dualpi2_target, "tupdate": args.dualpi2_tupdate, "step_thresh": args.dualpi2_step},
            },
            topology={
                "path": "server <-> s1 <-> client",
                "bottleneck": "symmetric HTB+DualPI2 on both s1 egress interfaces",
                "endpoint_delay": f"{args.base_rtt_ms / 2:g} ms each direction endpoint egress",
            },
            observed_network=capture_mininet_state(client, server, switch),
        )
        for repetition in range(1, args.repetitions + 1):
            for count in args.counts:
                print(f"FETCH concurrency: N={count}, repetition={repetition}", flush=True)
                summaries.append(run_case(server, client, bottlenecks, args.output, count, repetition, args))
                write_json(args.output / "run-summary.json", summaries)
    finally:
        net.stop()

    subprocess.run([
        "python3", str(ROOT / "tools" / "l4s" / "analyze_fetch_concurrency.py"),
        "--input", str(args.output), "--timeout", str(args.timeout), "--output", str(args.output),
    ], check=True)


if __name__ == "__main__":
    main()
