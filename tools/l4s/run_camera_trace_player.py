#!/usr/bin/env python3
"""Launch a fresh camera-trace Gaussian Player visualization and recording."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from experiment_metadata import write_experiment_record

ROOT = Path(__file__).resolve().parents[2]
PLAYER = ROOT / "deps/gaussian-player"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def validate_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise ValueError("manifest schema_version must be 2")
    if manifest.get("payload_encoding", "splat") not in ("splat", "gsp2"):
        raise ValueError("unsupported payload encoding")
    if manifest.get("tiling", {}).get("dimensions") != [10, 10, 3]:
        raise ValueError("camera-trace player requires a 10x10x3 spatial grid")
    if manifest.get("scene", {}).get("sampling") != "full":
        raise ValueError("camera-trace player requires full-scene sampling")
    tiles = manifest.get("tiles")
    if not isinstance(tiles, list) or len(tiles) != manifest.get("tiling", {}).get("occupied_tiles"):
        raise ValueError("occupied_tiles does not match the manifest tile count")
    files: list[str] = []
    tracks: list[str] = []
    refinement_total = 0
    for tile in tiles:
        refinements = tile.get("refinements")
        if not isinstance(refinements, list) or [item.get("index") for item in refinements] != [0, 1, 2, 3]:
            raise ValueError(f"tile {tile.get('id')} must contain refinement indices 0..3")
        count = sum(int(item.get("gaussian_count", -1)) for item in refinements)
        if count != tile.get("gaussian_count"):
            raise ValueError(f"tile {tile.get('id')} refinement Gaussian accounting is inconsistent")
        refinement_total += count
        for refinement in refinements:
            file_name = refinement.get("file")
            track = refinement.get("track")
            if not isinstance(file_name, str) or not isinstance(track, str):
                raise ValueError("every refinement requires string file and track fields")
            files.append(file_name); tracks.append(track)
            if not (path.parent / file_name).is_file():
                raise ValueError(f"missing refinement file: {file_name}")
    if len(files) != len(set(files)) or len(tracks) != len(set(tracks)):
        raise ValueError("refinement file and track names must be unique")
    if refinement_total != manifest.get("scene", {}).get("gaussian_count"):
        raise ValueError("scene Gaussian accounting is inconsistent")
    return manifest


def validate_trace(path: Path) -> list[dict[str, Any]]:
    trace = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(trace, list) or not trace:
        raise ValueError("camera trace must contain at least one frame")
    for index, frame in enumerate(trace):
        timestamp = frame.get("timestamp_ms")
        if not isinstance(timestamp, (int, float)) or (index == 0 and timestamp != 0) or (
                index > 0 and timestamp <= trace[index - 1]["timestamp_ms"]):
            raise ValueError("camera trace must contain strictly ordered timestamps starting at zero")
        required = ("camera_position", "view_matrix", "fov") if "view_matrix" in frame else (
            "camera_position", "camera_forward", "camera_up", "yaw_degrees", "fov", "aspect_ratio")
        if not all(key in frame for key in required):
            raise ValueError(f"camera trace frame {index} is incomplete")
    return trace


def browser_command(url: str, output: Path, telemetry: Path, *, timeout_ms: int,
                    width: int, height: int, headless: bool, record: bool) -> list[str]:
    command = ["node", str(ROOT / "tools/l4s/camera_trace_browser_runner.mjs"),
               "--url", url, "--telemetry", str(telemetry), "--timeout-ms", str(timeout_ms),
               "--width", str(width), "--height", str(height)]
    if record: command.extend(("--output", str(output)))
    else: command.append("--no-record")
    if headless: command.append("--headless")
    return command


def conversion_commands(webm: Path, output: Path, format_name: str, fps: int, width: int,
                        duration_ms: int) -> list[list[str]]:
    commands: list[list[str]] = []
    duration = f"{duration_ms / 1000:.3f}"
    if format_name in ("mp4", "all"):
        commands.append(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(webm),
                         "-vf", f"tpad=stop_mode=clone:stop_duration={duration},fps={fps}", "-t", duration,
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                         str(output / "camera-trace.mp4")])
    if format_name in ("gif", "all"):
        graph = (f"tpad=stop_mode=clone:stop_duration={duration},fps=15:round=up,"
                 f"scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse")
        commands.append(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(webm),
                         "-filter_complex", graph, "-t", duration, str(output / "camera-trace.gif")])
    return commands


def video_last_pts_seconds(path: Path) -> float:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True)
    timestamps = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    if not timestamps or max(timestamps) <= 0:
        raise RuntimeError(f"recording has no usable video timestamps: {path}")
    return max(timestamps)


def normalize_webm_command(raw: Path, webm: Path, fps: int, duration_ms: int,
                           source_last_pts: float) -> list[str]:
    duration = f"{duration_ms / 1000:.3f}"
    # Hold the final captured pose across the last two CFR slots. MediaRecorder's
    # final sparse frame otherwise has zero duration and the fps filter drops it.
    target_last_pts = duration_ms / 1000 - 2 / fps
    scale = target_last_pts / source_last_pts
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(raw),
            "-vf", f"setpts={scale:.12f}*PTS,tpad=stop_mode=clone:stop_duration=1,fps={fps}",
            "-t", duration,
            "-an", "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", str(webm)]


def wait_port(port: int, processes: list[subprocess.Popen[bytes]], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() not in (None, 0) for process in processes):
            raise RuntimeError("a playback component exited during startup")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"port {port} did not become ready")


def wait_file(path: Path, processes: list[subprocess.Popen[bytes]], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file(): return
        if any(process.poll() not in (None, 0) for process in processes):
            raise RuntimeError("a playback component exited before publisher readiness")
        time.sleep(0.05)
    raise TimeoutError(f"publisher readiness file was not created: {path}")


def terminate(processes: list[subprocess.Popen[bytes]]) -> dict[str, int | None]:
    for process in reversed(processes):
        if process.poll() is None: process.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 5
    for process in reversed(processes):
        try: process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
    return {str(process.args): process.returncode for process in processes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("webm", "mp4", "gif", "all"), default="webm")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--transport-mode", choices=("reno", "prague"), default="reno")
    parser.add_argument("--transport", choices=("http", "moqt"), default="moqt")
    parser.add_argument("--network-path", choices=("loopback", "qemu"), default="loopback")
    parser.add_argument("--fixed-frame", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--camera-check", action="store_true", help="validate rendered recorded/yaw poses after full loading")
    parser.add_argument("--qemu-base", type=Path, default=Path("~/sandbox/p4/work.qcow2"))
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--admission-delay-us", type=int, default=0)
    parser.add_argument("--bottleneck", choices=("10mbit", "20mbit", "50mbit", "100mbit"), default="20mbit")
    parser.add_argument("--no-dye", action="store_true")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--headless", action="store_true", help="CI/testing override; visible Chromium is the default")
    args = parser.parse_args()
    if args.interactive or args.fixed_frame or args.camera_check or args.network_path == "qemu" or args.transport == "http":
        if not args.no_record:
            parser.error("fixed-frame/QEMU validation requires --no-record")
        from run_player_validation import run
        return run(args)
    if min(args.width, args.height, args.fps) <= 0: parser.error("width, height, and fps must be positive")
    if args.no_record and args.format != "webm": parser.error("video conversion requires recording")
    manifest_path, trace_path, output = args.manifest.resolve(), args.trace.resolve(), args.output.resolve()
    results_root = (ROOT / "results").resolve()
    if not output.is_relative_to(results_root): parser.error("--output must be under the ignored results/ directory")
    if output.exists(): parser.error("--output must not already exist")
    manifest = validate_manifest(manifest_path)
    trace = validate_trace(trace_path)
    for required in (ROOT / "build/priority-moq-publisher", ROOT / "build/priority-moq-sidecar",
                     ROOT / "deps/imquic/examples/imquic-moq-relay", PLAYER / "dist/index.html"):
        if not required.is_file(): parser.error(f"missing build artifact {required}; run make build-3dgs-priority-mvp")
    if not args.no_record and shutil.which("ffmpeg") is None and args.format != "webm":
        parser.error("ffmpeg is required for MP4/GIF conversion")
    output.mkdir(parents=True)
    relay_port = free_port(socket.SOCK_DGRAM)
    bridge_port, web_port = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_STREAM)
    run_id = output.name
    raw_webm = output / "camera-trace.raw.webm"
    webm = output / "camera-trace.webm"
    telemetry_path = output / "player-telemetry.json"
    ready_path = output / "publisher-ready.json"
    processes: list[subprocess.Popen[bytes]] = []
    handles = []

    def start(name: str, command: list[str]) -> subprocess.Popen[bytes]:
        handle = (output / f"{name}.log").open("wb"); handles.append(handle)
        process = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    params = {"transport": "moqt", "sidecar": f"ws://127.0.0.1:{bridge_port}",
              "trace": "/camera-trace.json", "autoplay": "1", "dye": "0" if args.no_dye else "1",
              "record": "0" if args.no_record else "1", "recordWidth": str(args.width),
              "recordHeight": str(args.height), "recordFps": str(args.fps), "run": run_id,
              "nonce": f"{time.time_ns()}"}
    url = f"http://127.0.0.1:{web_port}/?{urlencode(params)}"
    configuration = {"manifest": str(manifest_path), "manifest_sha256": sha256(manifest_path),
        "trace": str(trace_path), "trace_sha256": sha256(trace_path), "trace_frames": len(trace),
        "trace_duration_ms": trace[-1]["timestamp_ms"], "dimensions": [10, 10, 3], "quality_layers": 4,
        "occupied_tiles": manifest["tiling"]["occupied_tiles"], "dye": not args.no_dye,
        "record": not args.no_record, "recording": {"width": args.width, "height": args.height,
        "fps": args.fps, "format": args.format}, "transport_mode": args.transport_mode,
        "ports": {"relay_udp": relay_port, "bridge_tcp": bridge_port, "web_tcp": web_port},
        "capture": {"pcap": False, "qlog": False}}
    browser_status = 1
    exit_codes: dict[str, int | None] = {}
    try:
        relay = start("relay", [str(ROOT / "deps/imquic/examples/imquic-moq-relay"), "-M", "19", "-q",
            "-b", "127.0.0.1", "-p", str(relay_port), "-c", str(ROOT / "deps/picoquic/certs/cert.pem"),
            "-k", str(ROOT / "deps/picoquic/certs/key.pem"), "-Z", "-T", "-d", "4"])
        time.sleep(0.3)
        if relay.poll() is not None: raise RuntimeError("relay exited during startup")
        publisher = start("publisher", [str(ROOT / "build/priority-moq-publisher"), "127.0.0.1",
            str(relay_port), str(manifest_path), str(ready_path)])
        wait_file(ready_path, [relay, publisher])
        bridge = start("bridge", ["node", str(ROOT / "tools/l4s/priority_ws_bridge.mjs"),
            "--native", str(ROOT / "build/priority-moq-sidecar"), "--relay-host", "127.0.0.1",
            "--relay-port", str(relay_port), "--listen", str(bridge_port), "--transport-mode",
            args.transport_mode, "--origin", f"http://127.0.0.1:{web_port}"])
        wait_port(bridge_port, [relay, publisher, bridge])
        web = start("web", [sys.executable, str(ROOT / "tools/l4s/camera_trace_player_server.py"),
            "--dist", str(PLAYER / "dist"), "--manifest", str(manifest_path), "--trace", str(trace_path),
            "--port", str(web_port)])
        wait_port(web_port, [relay, publisher, bridge, web])
        write_experiment_record(output, scenario="camera-trace-browser-visualization", configuration=configuration,
            topology="visible Chromium -> loopback WebSocket bridge -> native IMQUIC sidecar -> relay <- preloaded publisher",
            observed_network={"processes": {"relay_pid": relay.pid, "publisher_pid": publisher.pid,
                "bridge_pid": bridge.pid, "web_pid": web.pid}})
        timeout_ms = int(trace[-1]["timestamp_ms"]) + 120_000
        browser = start("browser", browser_command(url, raw_webm, telemetry_path, timeout_ms=timeout_ms,
            width=args.width, height=args.height, headless=args.headless, record=not args.no_record))
        browser_status = browser.wait(timeout=timeout_ms / 1000 + 30)
    except Exception as error:
        (output / "failure.txt").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
    finally:
        exit_codes = terminate(processes)
        for handle in handles: handle.close()

    if browser_status == 0 and not args.no_record:
        source_last_pts = video_last_pts_seconds(raw_webm)
        subprocess.run(normalize_webm_command(raw_webm, webm, args.fps,
            int(trace[-1]["timestamp_ms"]), source_last_pts), cwd=ROOT, check=True)
        for command in conversion_commands(webm, output, args.format, args.fps, args.width,
                                           int(trace[-1]["timestamp_ms"])):
            subprocess.run(command, cwd=ROOT, check=True)
        raw_webm.unlink()
    artifacts = {path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
                 for path in (webm, output / "camera-trace.mp4", output / "camera-trace.gif") if path.is_file()}
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8")) if telemetry_path.is_file() else None
    summary = {"visualization_only": True, "browser_exit_code": browser_status,
        "process_exit_codes": exit_codes, "artifacts": artifacts,
        "delivery": None if telemetry is None else {key: telemetry.get(key) for key in
            ("sourceGaussians", "receivedGaussians", "appliedGaussians", "renderedMeshes", "duplicates", "rejected")},
        "trace": None if telemetry is None else telemetry.get("trace"),
        "recording": None if telemetry is None else telemetry.get("recording")}
    (output / "visualization-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    success = browser_status == 0 and telemetry is not None and telemetry.get("trace", {}).get("state") == "complete"
    if not args.no_record: success = success and webm.is_file() and webm.stat().st_size > 0
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
