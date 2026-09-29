#!/usr/bin/env python3
"""Unprivileged synthetic gate for accepted priority updates on pending objects."""

from __future__ import annotations

import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from analyze_camera_priority import validate_object_coverage
from run_qemu_camera_priority import make_scene, preflight_source, preflight_trace

ROOT = Path(__file__).resolve().parents[2]


def free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait()


def wait_file(path: Path, processes: list[subprocess.Popen[bytes]], timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if any(process.poll() not in (None, 0) for process in processes):
            raise RuntimeError("loopback component exited before publisher preload")
        time.sleep(0.05)
    raise TimeoutError(path)


def main() -> None:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output = ROOT / "results/l4s" / f"priority-audit-loopback-{stamp}"
    output.mkdir(parents=True)
    source = output / "synthetic.splat"
    preflight_source(source, 4 * 1024 * 1024)
    manifest_path = make_scene(source, output / "scene", "1x1x1", "0.1,0.2,0.3,0.4")
    trace_path = output / "trace.json"
    preflight_trace(trace_path, 31)
    certificate, key_path = output / "cert.pem", output / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-subj", "/CN=127.0.0.1", "-keyout", key_path, "-out", certificate, "-days", "1"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    relay_port, bridge_port = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_STREAM)
    processes: list[subprocess.Popen[bytes]] = []
    handles = []

    def start(name: str, command: list[str], env: dict[str, str] | None = None):
        handle = (output / f"{name}.log").open("wb"); handles.append(handle)
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle,
                                   stderr=subprocess.STDOUT); processes.append(process)
        return process

    try:
        relay_env = {**os.environ, "IMQUIC_RELAY_ADMISSION_DELAY_US": "50000"}
        relay = start("relay", [str(ROOT / "deps/imquic/examples/imquic-moq-relay"),
            "-M", "19", "-q", "-b", "127.0.0.1", "-p", str(relay_port), "-c",
            str(certificate), "-k", str(key_path), "-Z", "--congestion-control", "prague",
            "--transport-metrics", str(output / "transport-metrics.csv")], relay_env)
        time.sleep(0.3)
        ready = output / "publisher-ready.json"
        publisher = start("publisher", [str(ROOT / "build/priority-moq-publisher"),
            "127.0.0.1", str(relay_port), str(manifest_path), str(ready)])
        wait_file(ready, [relay, publisher])
        bridge = start("bridge", ["node", str(ROOT / "tools/l4s/priority_ws_bridge.mjs"),
            "--native", str(ROOT / "build/priority-moq-sidecar"), "--relay-host", "127.0.0.1",
            "--relay-port", str(relay_port), "--listen", str(bridge_port),
            "--transport-mode", "prague", "--origin", ""])
        time.sleep(0.3)
        controller = start("controller", ["node", "--experimental-websocket",
            str(ROOT / "deps/gaussian-player/dist-headless/headless-priority-client.js"),
            "--manifest", str(manifest_path), "--trace", str(trace_path), "--sidecar",
            f"ws://127.0.0.1:{bridge_port}", "--output", str(output)])
        if controller.wait(timeout=30) != 0:
            raise RuntimeError("headless loopback controller failed")
    finally:
        for process in reversed(processes):
            stop(process)
        for handle in handles:
            handle.close()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with (output / "object-deliveries.csv").open(newline="", encoding="utf-8") as stream:
        deliveries = list(csv.DictReader(stream))
    validate_object_coverage(manifest, deliveries)
    with (output / "object-priority-history.csv").open(newline="", encoding="utf-8") as stream:
        histories = list(csv.DictReader(stream))
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in histories:
        grouped[(row["tile_id"], row["refinement"], row["object_id"])].append(row)
    qualifying = [rows for rows in grouped.values()
                  if sum(row["kind"] == "accepted" for row in rows) >= 2]
    if not qualifying:
        raise RuntimeError("no still-undelivered object accumulated multiple accepted priority updates")
    result = {"validated": True, "epochs": 31, "objects_with_multiple_accepted_updates": len(qualifying),
              "delivered": sum(row["status"] == "delivered" for row in deliveries),
              "undelivered": sum(row["status"] == "undelivered" for row in deliveries)}
    (output / "audit-gate.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"priority audit loopback: PASS ({output})")


if __name__ == "__main__":
    main()
