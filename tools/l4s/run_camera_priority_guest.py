#!/usr/bin/env python3
"""Run the camera-driven priority experiment in fresh Mininet topologies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from .experiment_metadata import capture_mininet_state, write_experiment_record
except ImportError:
    from experiment_metadata import capture_mininet_state, write_experiment_record

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "deps/imquic/examples/imquic-moq-relay"
PUBLISHER = ROOT / "build/priority-moq-publisher"
BRIDGE = ROOT / "tools/l4s/priority_ws_bridge.mjs"
SIDECAR = ROOT / "build/priority-moq-sidecar"
HEADLESS = ROOT / "deps/gaussian-player/dist-headless/headless-priority-client.js"
NODE = os.environ.get("CAMERA_PRIORITY_NODE", "node")


def case_order() -> list[tuple[str, int]]:
    return [("prague", 1), ("reno", 1), ("reno", 2),
            ("prague", 2), ("prague", 3), ("reno", 3)]


def shaping_commands(device: str) -> list[list[str]]:
    return [
        ["tc", "qdisc", "replace", "dev", device, "root", "handle", "1:",
         "netem", "delay", "20ms", "limit", "100000"],
        ["tc", "qdisc", "replace", "dev", device, "parent", "1:1", "handle",
         "10:", "htb", "default", "1"],
        ["tc", "class", "replace", "dev", device, "parent", "10:", "classid",
         "10:1", "htb", "rate", "20mbit", "burst", "32k", "cburst", "32k"],
        ["tc", "qdisc", "replace", "dev", device, "parent", "10:1", "handle",
         "20:", "dualpi2", "target", "15ms", "tupdate", "16ms",
         "step_thresh", "1ms"],
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stop(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def wait_path(path: Path, processes: list[subprocess.Popen[Any]], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        failed = [process for process in processes if process.poll() not in (None, 0)]
        if failed:
            raise RuntimeError(f"process exited while waiting for {path.name}: {failed[0].returncode}")
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {path}")


def tc_state(node: Any | None, device: str) -> str:
    command = f"tc -s -d qdisc show dev {device}; tc -s -d class show dev {device}"
    if node is not None:
        return node.cmd(command)
    return subprocess.run(["bash", "-lc", command], check=True, text=True,
                          stdout=subprocess.PIPE).stdout


def counters(node: Any | None, device: str) -> dict[str, Any]:
    command = ["ip", "-s", "-j", "link", "show", "dev", device]
    if node is None:
        text = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE).stdout
    else:
        text = node.cmd(" ".join(command))
    return json.loads(text)[0]


def configure_device(node: Any | None, device: str) -> None:
    for command in shaping_commands(device):
        if node is None:
            subprocess.run(command, check=True)
        else:
            result = node.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
            output, _ = result.communicate()
            if result.returncode:
                raise RuntimeError(f"{' '.join(command)}: {output}")


def write_tc(case: Path, label: str, devices: dict[str, tuple[Any | None, str]]) -> None:
    with (case / f"tc-state-{label}.txt").open("w", encoding="utf-8") as stream:
        for name, (node, device) in devices.items():
            stream.write(f"[{name}] device={device}\n{tc_state(node, device)}\n")


def start(node: Any, command: list[str], log: Path, *, env: dict[str, str] | None = None,
          stdout_json: bool = False) -> tuple[subprocess.Popen[Any], Any]:
    mode = "w" if stdout_json else "wb"
    handle = log.open(mode)
    process = node.popen(command, cwd=ROOT, env=env, stdout=handle,
                         stderr=subprocess.STDOUT)
    return process, handle


def validate_background(path: Path, minimum_seconds: float) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    end = data.get("end", {})
    if end.get("sender_tcp_congestion") != "reno":
        raise RuntimeError(f"{path}: background flow did not use Reno")
    seconds = float(end.get("sum_sent", {}).get("seconds", 0))
    if seconds < minimum_seconds:
        raise RuntimeError(f"{path}: background flow terminated after {seconds}s")


def run_case(root: Path, manifest_path: Path, trace_path: Path, mode: str,
             repetition: int, certificate: Path, key: Path, preflight: bool) -> dict[str, Any]:
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge

    suffix = "preflight" if preflight else f"{mode}-rep-{repetition:02d}"
    case = root / suffix
    case.mkdir(parents=True, exist_ok=False)
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    duration = float(trace[-1]["timestamp_ms"]) / 1000
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    switch = net.addSwitch("s1", failMode="standalone")
    server = net.addHost("server", ip="10.0.0.2/24")
    client_link = net.addLink(client, switch)
    server_link = net.addLink(switch, server)
    processes: list[subprocess.Popen[Any]] = []
    handles: list[Any] = []
    try:
        net.start()
        subprocess.run(["modprobe", "sch_dualpi2"], check=True)
        devices = {
            "client_to_s1": (client, client_link.intf1.name),
            "s1_to_client": (None, client_link.intf2.name),
            "s1_to_server": (None, server_link.intf1.name),
            "server_to_s1": (server, server_link.intf2.name),
        }
        for node, device in devices.values():
            configure_device(node, device)
        write_tc(case, "before", devices)
        (case / "interface-counters-before.json").write_text(json.dumps({
            name: counters(node, device) for name, (node, device) in devices.items()
        }, indent=2) + "\n", encoding="utf-8")
        observed = capture_mininet_state(client, server, switch)
        ping = client.cmd(f"ping -c 3 -W 2 {server.IP()}")
        (case / "ping.txt").write_text(ping, encoding="utf-8")
        if "0% packet loss" not in ping:
            raise RuntimeError("client-server topology ping failed")

        for host in (client, server):
            host.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
            host.cmd("iptables -t mangle -F OUTPUT")
            host.cmd("iptables -t mangle -A OUTPUT -p tcp -j TOS --set-tos 0x00")

        relay, handle = start(server, [str(RELAY), "-M", "19", "-q", "-b", server.IP(),
            "-p", "4443", "-c", str(certificate), "-k", str(key), "-Z", "-T",
            "-d", "4", "--congestion-control", mode, "--transport-metrics",
            str(case / "transport-metrics.csv")], case / "relay.log")
        processes.append(relay); handles.append(handle)
        time.sleep(0.5)
        if relay.poll() is not None:
            raise RuntimeError("relay exited during startup")
        ready = case / "publisher-ready.json"
        publisher, handle = start(server, [str(PUBLISHER), server.IP(), "4443",
            str(manifest_path), str(ready)], case / "publisher.log")
        processes.append(publisher); handles.append(handle)
        wait_path(ready, [relay, publisher], 180)

        bridge, handle = start(client, [NODE, str(BRIDGE), "--native", str(SIDECAR),
            "--relay-host", server.IP(), "--relay-port", "4443", "--listen", "8787",
            "--transport-mode", mode, "--origin", ""], case / "bridge.log")
        processes.append(bridge); handles.append(handle)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "8787" not in client.cmd("ss -ltn"):
            if bridge.poll() is not None:
                raise RuntimeError("bridge exited during startup")
            time.sleep(0.1)
        else:
            if "8787" not in client.cmd("ss -ltn"):
                raise TimeoutError("bridge did not listen")

        background_duration = duration + 4
        iperf_server_rx, handle = start(server, ["iperf3", "-s", "-1", "-p", "5201"],
                                          case / "iperf-server-client-to-server.log")
        processes.append(iperf_server_rx); handles.append(handle)
        iperf_server_tx, handle = start(client, ["iperf3", "-s", "-1", "-p", "5202"],
                                          case / "iperf-server-server-to-client.log")
        processes.append(iperf_server_tx); handles.append(handle)
        time.sleep(0.2)
        client_to_server, handle = start(client, ["iperf3", "-c", server.IP(), "-p", "5201",
            "-t", f"{background_duration:g}", "-b", "10M", "-C", "reno", "--json"],
            case / "iperf-client-to-server.json", stdout_json=True)
        processes.append(client_to_server); handles.append(handle)
        server_to_client, handle = start(server, ["iperf3", "-c", client.IP(), "-p", "5202",
            "-t", f"{background_duration:g}", "-b", "10M", "-C", "reno", "--json"],
            case / "iperf-server-to-client.json", stdout_json=True)
        processes.append(server_to_client); handles.append(handle)
        time.sleep(2)

        environment = {**os.environ, "NODE_NO_WARNINGS": "1"}
        controller, handle = start(client, [NODE, "--experimental-websocket", str(HEADLESS),
            "--manifest", str(manifest_path), "--trace", str(trace_path),
            "--sidecar", "ws://127.0.0.1:8787", "--output", str(case)],
            case / "controller.log", env=environment)
        processes.append(controller); handles.append(handle)
        if controller.wait(timeout=duration + 45) != 0:
            raise RuntimeError("headless priority controller failed")
        for flow in (client_to_server, server_to_client):
            if flow.wait(timeout=15) != 0:
                raise RuntimeError("background flow failed")
        validate_background(case / "iperf-client-to-server.json", background_duration - 1)
        validate_background(case / "iperf-server-to-client.json", background_duration - 1)
        write_tc(case, "after", devices)
        (case / "interface-counters-after.json").write_text(json.dumps({
            name: counters(node, device) for name, (node, device) in devices.items()
        }, indent=2) + "\n", encoding="utf-8")
        metadata = {"mode": mode, "controller": "prague" if mode == "prague" else "reno",
            "ecn": "ect1" if mode == "prague" else "not-ect", "repetition": repetition,
            "duration_seconds": duration, "background_target_mbps_each_direction": 10,
            "background_congestion": "reno", "background_ecn": "not-ect",
            "topology": "client-s1-server", "base_rtt_expected_ms": 80,
            "observed_network": observed}
        (case / "case-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n",
                                                   encoding="utf-8")
        return observed
    finally:
        for process in reversed(processes):
            stop(process)
        for handle in handles:
            handle.close()
        net.stop()
        subprocess.run(["mn", "-c"], check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("camera priority guest runner must run as root")
    for path in (args.manifest, args.trace, RELAY, PUBLISHER, BRIDGE, SIDECAR, HEADLESS):
        if not path.exists():
            raise SystemExit(f"missing input: {path}")
    args.output.mkdir(parents=True, exist_ok=False)
    certificate, key = args.output / "cert.pem", args.output / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-subj", "/CN=10.0.0.2", "-keyout", key, "-out", certificate, "-days", "1"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    observed: dict[str, Any] = {}
    try:
        cases = [("prague", 1)] if args.preflight else case_order()
        for mode, repetition in cases:
            observed = run_case(args.output, args.manifest.resolve(), args.trace.resolve(),
                                mode, repetition, certificate, key, args.preflight)
        write_experiment_record(args.output, scenario="camera-driven-3dgs-priority-qemu",
            configuration={"hypothesis": "Prague reduces accepted-update-to-delivery latency and outside-viewport stale delivery relative to Reno",
                "manifest": str(args.manifest.resolve()), "manifest_sha256": sha256(args.manifest),
                "trace": str(args.trace.resolve()), "trace_sha256": sha256(args.trace),
                "node_runtime": NODE, "node_runtime_sha256": sha256(Path(NODE)) if Path(NODE).is_file() else "system-path",
                "preflight": args.preflight, "case_order": cases, "epochs_required": 501 if not args.preflight else len(json.loads(args.trace.read_text())),
                "grid": [10, 10, 3] if not args.preflight else "synthetic",
                "lod_fractions": [0.1, 0.2, 0.3, 0.4], "fov_degrees": 40,
                "qdisc": {"rate": "20mbit", "delay_each_directed_side": "20ms",
                    "burst": "32k", "cburst": "32k", "target": "15ms",
                    "tupdate": "16ms", "step_thresh": "1ms"},
                "background": {"directions": 2, "target_mbps": 10, "cc": "reno",
                    "ecn": "not-ect", "warmup_seconds": 2, "drain_seconds": 2},
                "capture": {"video": False, "ssim": False, "pcap": False, "qlog": False}},
            topology={"path": "client - s1 - server", "client": "headless+bridge+subscriber",
                "server": "relay+preloaded publisher", "shaped_egresses": 4},
            observed_network=observed)
    finally:
        certificate.unlink(missing_ok=True)
        key.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
