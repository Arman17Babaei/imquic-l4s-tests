#!/usr/bin/env python3
"""Prepare inputs, run the synthetic preflight, then run and promote six QEMU cases."""

from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAYER = ROOT / "deps/gaussian-player"
SPLITTER = PLAYER / "split-splat.mjs"
QEMU = ROOT / "tools/l4s/run_qemu_timeseries_test.py"
ANALYZER = ROOT / "tools/l4s/analyze_camera_priority.py"


def manifest_valid(path: Path, source: Path, grid: list[int], fractions: list[float]) -> bool:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (manifest.get("tiling", {}).get("dimensions") == grid and
            manifest.get("progression", {}).get("layer_fractions") == fractions and
            manifest.get("scene", {}).get("sampling") == "full" and
            manifest.get("scene", {}).get("gaussian_count") == source.stat().st_size // 32)


def make_scene(source: Path, destination: Path, grid: str, fractions: str) -> Path:
    manifest = destination / "manifest-v2.json"
    parsed_grid = [int(value) for value in grid.split("x")]
    parsed_fractions = [float(value) for value in fractions.split(",")]
    if manifest_valid(manifest, source, parsed_grid, parsed_fractions):
        return manifest
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(["node", str(SPLITTER), str(source), "--output", str(destination),
                    "--grid", grid, "--layer-fractions", fractions,
                    "--scene-id", "bicycle" if source.name == "bicycle.splat" else "synthetic"],
                   cwd=ROOT, check=True)
    return manifest


def scene_archive(manifest_path: Path, destination: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = [manifest_path, *(manifest_path.parent / refinement["file"]
        for tile in manifest["tiles"] for refinement in tile["refinements"])]
    with tarfile.open(destination, "w") as archive:
        for path in files:
            archive.add(path, arcname=path.name, recursive=False)


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
            "yaw_degrees": yaw, "fov": 40, "aspect_ratio": 16 / 9})
    path.write_text(json.dumps(frames, indent=2) + "\n", encoding="utf-8")


def qemu_command(args: argparse.Namespace, archive: Path, trace: Path, runtime: Path,
                 node: Path, *, preflight: bool, destination_prefix: str) -> list[str]:
    guest_name = "camera-priority-preflight" if preflight else "camera-priority"
    command = [sys.executable, str(QEMU), "--base", str(args.base), "--make-target",
        "l4s-camera-priority-guest-check", "--guest-result-name", guest_name,
        "--guest-result-root", "results/l4s", "--destination-root", "results/l4s",
        "--destination-prefix", destination_prefix, "--guest-file",
        f"{archive}=inputs/camera-priority-scene.tar", "--guest-file",
        f"{trace}=inputs/camera-priority-trace.json", "--make-variable",
        "CAMERA_PRIORITY_SCENE_ARCHIVE=inputs/camera-priority-scene.tar", "--make-variable",
        "CAMERA_PRIORITY_TRACE=inputs/camera-priority-trace.json", "--guest-file",
        f"{runtime}=inputs/camera-priority-runtime.tar.gz", "--guest-file",
        f"{node}=inputs/node", "--make-variable",
        "CAMERA_PRIORITY_RUNTIME_ARCHIVE=inputs/camera-priority-runtime.tar.gz", "--make-variable",
        "CAMERA_PRIORITY_NODE=inputs/node", "--memory",
        str(args.memory), "--cpus", str(args.cpus), "--guest-timeout",
        str(args.guest_timeout), "--allow-dirty"]
    if preflight:
        command.extend(["--make-variable", "CAMERA_PRIORITY_PREFLIGHT=1"])
    return command


def new_result(prefix: str, previous: set[Path]) -> Path:
    candidates = set((ROOT / "results/l4s").glob(f"{prefix}-*")) - previous
    if len(candidates) != 1:
        raise RuntimeError(f"expected one new {prefix} result, found {sorted(candidates)}")
    return candidates.pop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("~/sandbox/p4/work.qcow2").expanduser())
    parser.add_argument("--source", type=Path, default=ROOT / "results/3dgs/bicycle.splat")
    parser.add_argument("--camera-source", type=Path,
                        default=ROOT / "deps/3dgs_over_moq/assets/user102_bicycle.json")
    parser.add_argument("--memory", type=int, default=6144)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--guest-timeout", type=int, default=3600)
    args = parser.parse_args()
    for path in (args.base, args.source, args.camera_source):
        if not path.is_file():
            raise SystemExit(f"missing input: {path}")
    node_text = shutil.which("node")
    if node_text is None:
        raise SystemExit("host Node.js is required")
    node = Path(node_text).resolve()

    full_dir = ROOT / "results/3dgs/camera-priority-bicycle"
    full_manifest = make_scene(args.source.resolve(), full_dir, "10x10x3", "0.1,0.2,0.3,0.4")
    full_trace = full_dir / "camera-trace.json"
    subprocess.run([sys.executable, str(ROOT / "tools/l4s/generate_camera_priority_trace.py"),
                    "--source", str(args.camera_source), "--output", str(full_trace)], check=True)
    subprocess.run(["npm", "run", "build"], cwd=PLAYER, check=True)

    with tempfile.TemporaryDirectory(prefix="camera-priority-qemu-") as temporary_text:
        temporary = Path(temporary_text)
        runtime_tar = temporary / "camera-priority-runtime.tar.gz"
        with tarfile.open(runtime_tar, "w:gz") as runtime:
            runtime.add(PLAYER / "dist-headless", arcname="dist-headless")
            runtime.add(PLAYER / "node_modules/three", arcname="node_modules/three")
        preflight_dir = temporary / "preflight-scene"
        preflight_source_path = temporary / "preflight.splat"
        preflight_source(preflight_source_path)
        preflight_manifest = make_scene(preflight_source_path, preflight_dir, "1x1x1", "0.1,0.2,0.3,0.4")
        preflight_trace_path = temporary / "preflight-trace.json"
        preflight_trace(preflight_trace_path)
        preflight_tar = temporary / "preflight-scene.tar"
        scene_archive(preflight_manifest, preflight_tar)
        preflight_prefix = "qemu-camera-priority-preflight"
        before = set((ROOT / "results/l4s").glob(f"{preflight_prefix}-*"))
        subprocess.run(qemu_command(args, preflight_tar, preflight_trace_path, runtime_tar, node,
                                    preflight=True, destination_prefix=preflight_prefix), cwd=ROOT, check=True)
        preflight_result = new_result(preflight_prefix, before)
        subprocess.run([sys.executable, str(ANALYZER), "--run", str(preflight_result),
                        "--manifest", str(preflight_manifest), "--epochs", "31", "--preflight"],
                       cwd=ROOT, check=True)

        full_tar = temporary / "bicycle-scene.tar"
        scene_archive(full_manifest, full_tar)
        full_prefix = "qemu-camera-priority-full"
        before = set((ROOT / "results/l4s").glob(f"{full_prefix}-*"))
        subprocess.run(qemu_command(args, full_tar, full_trace, runtime_tar, node, preflight=False,
                                    destination_prefix=full_prefix), cwd=ROOT, check=True)
        full_result = new_result(full_prefix, before)

    subprocess.run([sys.executable, str(ANALYZER), "--run", str(full_result),
                    "--manifest", str(full_manifest), "--epochs", "501"], cwd=ROOT, check=True)
    aggregate = json.loads((full_result / "aggregate-summary.json").read_text(encoding="utf-8"))
    conclusion = aggregate["classification"]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    result_text = json.dumps(aggregate["modes"], sort_keys=True)
    promote = [sys.executable, str(ROOT / "tools/promote_result.py"), str(full_result),
        "--id", f"camera-priority-qemu-{stamp}", "--title", "Camera-driven 3DGS priority over Prague and Reno",
        "--question", "Does Prague reduce accepted-priority-update-to-delivery latency and outside-viewport stale delivery relative to Reno?",
        "--hypothesis", "Prague reduces both metrics under the fixed bicycle trace and bidirectional Reno load.",
        "--result", result_text, "--conclusion", conclusion,
        "--paper-claim", "Use only the three-repetition median and min-max evidence preserved by this record.",
        "--caveats", "Accepted priority state does not prove retroactive reordering of bytes already committed to QUIC streams."]
    subprocess.run(promote, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
