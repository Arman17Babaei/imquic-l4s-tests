#!/usr/bin/env python3
"""Unprivileged synthetic gate for accepted priority updates on pending objects."""

from __future__ import annotations

import csv
import json
import math
import struct
import os
import signal
import socket
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def preflight_source(path: Path, bytes_total: int = 8 * 1024 * 1024) -> None:
    records = bytes_total // 32
    data = bytearray(records * 32)
    for index in range(records):
        angle = 2 * math.pi * index / records
        radius = 0.5 + (index % 101) / 400
        struct.pack_into("<fff", data, index * 32, radius * math.cos(angle),
                         ((index % 257) / 256 - 0.5), radius * math.sin(angle))
        data[index * 32 + 27] = 255 - index % 128
    path.write_bytes(data)

def preflight_trace(path: Path, epochs: int = 31) -> None:
    frames = []
    for epoch in range(epochs):
        yaw = -360 * epoch / (epochs - 1)
        angle = math.radians(yaw)
        frames.append({"timestamp_ms": epoch * 100, "camera_position": [0, 0, 3],
            "camera_forward": [-math.sin(angle), 0, -math.cos(angle)],
            "camera_up": [0, 1, 0],
            "yaw_degrees": yaw, "fov": 40, "aspect_ratio": 16 / 9})
    path.write_text(json.dumps(frames, indent=2) + "\n", encoding="utf-8")

def key(row: dict[str, Any]) -> tuple[str, int, int]:
    return str(row["tile_id"]), int(row["refinement"]), int(row["object_id"])

def expected_objects(manifest: dict[str, Any]) -> dict[tuple[str, int, int], dict[str, Any]]:
    return {(tile["id"], int(refinement["index"]), int(obj["id"])): obj
            for tile in manifest["tiles"] for refinement in tile["refinements"]
            for obj in refinement["objects"]}

def validate_object_coverage(manifest: dict[str, Any], rows: list[dict[str, str]]) -> None:
    expected = expected_objects(manifest)
    actual: dict[tuple[str, int, int], dict[str, str]] = {}
    for row in rows:
        row_key = key(row)
        if row_key in actual:
            raise ValueError(f"duplicate object delivery row {row_key}")
        actual[row_key] = row
    if set(actual) != set(expected):
        raise ValueError(f"manifest coverage mismatch missing={set(expected)-set(actual)} extra={set(actual)-set(expected)}")
    for row_key, row in actual.items():
        obj = expected[row_key]
        if int(row["payload_bytes"]) != int(obj["bytes"]):
            raise ValueError(f"byte mismatch for {row_key}")
        if row["sha256"] != obj["sha256"]:
            raise ValueError(f"manifest hash mismatch for {row_key}")
        if row["status"] == "delivered":
            if row["actual_sha256"] != obj["sha256"]:
                raise ValueError(f"delivered hash mismatch for {row_key}")
            if not row["request_id"] or not row["monotonic_us"]:
                raise ValueError(f"delivered object lacks request/time mapping {row_key}")
        elif row["status"] != "undelivered":
            raise ValueError(f"invalid terminal status for {row_key}")

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
    manifest_path = output / "scene/manifest-v2.json"
    subprocess.run(["node", str(ROOT / "deps/gaussian-player/split-splat.mjs"), str(source),
        "--output", str(manifest_path.parent), "--grid", "1x1x1",
        "--layer-fractions", "0.1,0.2,0.3,0.4", "--scene-id", "synthetic"], check=True)
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
