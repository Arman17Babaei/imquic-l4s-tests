#!/usr/bin/env python3
"""Run the unprivileged publisher-relay-sidecar-player MVP and preserve compact evidence."""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def revision(path: Path) -> str:
    return subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip()


def wait_port(port: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"port {port} did not become ready")


def start(name: str, command: list[str], log_dir: Path, processes: list[subprocess.Popen[bytes]]) -> subprocess.Popen[bytes]:
    log = (log_dir / f"{name}.log").open("wb")
    process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    process._evidence_log = log  # type: ignore[attr-defined]
    processes.append(process)
    return process


def terminate(processes: list[subprocess.Popen[bytes]]) -> dict[str, int | None]:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 5
    for process in reversed(processes):
        try:
            process.wait(max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process._evidence_log.close()  # type: ignore[attr-defined]
    return {str(process.args): process.returncode for process in processes}


def browser_probe_command(url: str, output: Path, expected_meshes: int, timeout_seconds: int) -> list[str]:
    if timeout_seconds <= 0:
        raise ValueError("probe timeout must be positive")
    return ["node", str(ROOT / "tools/l4s/priority_moq_browser_probe.mjs"), url,
        str(output), str(expected_meshes), str(timeout_seconds * 1000)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--relay-port", type=int, default=4443)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--ws-port", type=int, default=8787)
    parser.add_argument("--probe-timeout", type=int, default=180,
        help="browser-probe timeout in seconds (default: 180)")
    args = parser.parse_args()
    if args.probe_timeout <= 0:
        parser.error("--probe-timeout must be positive")
    output = args.output.resolve()
    results_root = (ROOT / "results").resolve()
    if not output.is_relative_to(results_root):
        parser.error("--output must be under the repository's ignored results/ directory")
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    expected_meshes = sum(refinement["object_count"] > 0 for tile in manifest["tiles"] for refinement in tile["refinements"])
    stage = output / "site"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(ROOT / "deps/gaussian-player/dist", stage)
    shutil.copy2(manifest_path, stage / "manifest-v2.json")
    for tile in manifest["tiles"]:
        for refinement in tile["refinements"]:
            shutil.copy2(manifest_path.parent / refinement["file"], stage / refinement["file"])

    processes: list[subprocess.Popen[bytes]] = []
    started = time.time_ns()
    statuses: dict[str, int | None] = {}
    probe_returncode = 1
    try:
        relay = start("relay", ["stdbuf", "-oL", str(ROOT / "deps/imquic/examples/imquic-moq-relay"),
            "-M", "19", "-q", "-b", "127.0.0.1", "-p", str(args.relay_port),
            "-c", str(ROOT / "deps/picoquic/certs/cert.pem"), "-k", str(ROOT / "deps/picoquic/certs/key.pem"),
            "-Z", "-T", "-d", "4"], output, processes)
        time.sleep(0.3)  # Raw QUIC is UDP, so a TCP readiness probe is not applicable.
        if relay.poll() is not None:
            raise RuntimeError("relay exited during startup")
        publisher = start("publisher", [str(ROOT / "build/priority-moq-publisher"), "127.0.0.1", str(args.relay_port), str(manifest_path)], output, processes)
        time.sleep(1)
        if publisher.poll() not in (None, 0):
            raise RuntimeError("publisher exited during preload")
        bridge = start("bridge", ["node", str(ROOT / "tools/l4s/priority_ws_bridge.mjs"),
            "--native", str(ROOT / "build/priority-moq-sidecar"), "--relay-host", "127.0.0.1",
            "--relay-port", str(args.relay_port), "--listen", str(args.ws_port),
            "--origin", f"http://127.0.0.1:{args.web_port}"], output, processes)
        wait_port(args.ws_port)
        web = start("web", ["python3", "-m", "http.server", str(args.web_port), "--bind", "127.0.0.1", "--directory", str(stage)], output, processes)
        wait_port(args.web_port)
        probe = subprocess.run(browser_probe_command(
            f"http://127.0.0.1:{args.web_port}/?transport=moqt&sidecar=ws://127.0.0.1:{args.ws_port}",
            output / "player-telemetry.json", expected_meshes, args.probe_timeout),
            cwd=ROOT, capture_output=True, text=True, timeout=args.probe_timeout + 30)
        (output / "browser.log").write_text(probe.stdout + probe.stderr)
        probe_returncode = probe.returncode
    except (subprocess.TimeoutExpired, OSError) as error:
        (output / "browser.log").write_text(str(error) + "\n")
    finally:
        statuses = terminate(processes)

    telemetry_path = output / "player-telemetry.json"
    telemetry = json.loads(telemetry_path.read_text()) if telemetry_path.exists() else {
        "sourceGaussians": 0, "receivedGaussians": 0, "appliedGaussians": 0,
        "duplicates": 0, "rejected": 0, "renderedMeshes": 0, "stages": [], "events": [],
        "failure": "browser probe produced no telemetry",
    }
    delivery = {"source_gaussians": telemetry["sourceGaussians"], "received_gaussians": telemetry["receivedGaussians"],
        "duplicates": telemetry["duplicates"], "rejected": telemetry["rejected"],
        "correct": telemetry["sourceGaussians"] == telemetry["receivedGaussians"] and telemetry["duplicates"] == telemetry["rejected"] == 0}
    rendering = {"applied_gaussians": telemetry["appliedGaussians"], "rendered_meshes": telemetry["renderedMeshes"],
        "expected_meshes": expected_meshes, "stages": len(telemetry["stages"]),
        "correct": telemetry["appliedGaussians"] == telemetry["sourceGaussians"] and telemetry["renderedMeshes"] == expected_meshes}
    priority_events = [event for event in telemetry["events"] if event.get("type") == "priorities-sent"]
    update_states = [event.get("state") for event in telemetry["events"] if event.get("type") == "fetch-status"]
    scheduling = {"priority_epochs": len(priority_events), "update_states": update_states,
        "camera_turn_recorded": any(event.get("type") == "camera-turn" for event in telemetry["events"]),
        "correct": len(priority_events) > 0 and "updating" in update_states and "accepted" in update_states}
    (output / "delivery-correctness.json").write_text(json.dumps(delivery, indent=2) + "\n")
    (output / "rendering-correctness.json").write_text(json.dumps(rendering, indent=2) + "\n")
    (output / "scheduling-correctness.json").write_text(json.dumps(scheduling, indent=2) + "\n")
    provenance = {"started_unix_ns": started, "finished_unix_ns": time.time_ns(), "manifest": str(manifest_path),
        "manifest_source_sha256": manifest["scene"]["source_sha256"], "grid": manifest["tiling"]["dimensions"],
        "object_bytes": manifest["tiles"][0]["refinements"][0]["object_bytes"], "draft": 19,
        "probe_timeout_seconds": args.probe_timeout,
        "qlog": False, "pcap": False, "process_exit_codes": statuses,
        "revisions": {"top": revision(ROOT), "gaussian-player": revision(ROOT / "deps/gaussian-player"),
            "imquic": revision(ROOT / "deps/imquic"), "picoquic": revision(ROOT / "deps/picoquic")},
        "promotable": delivery["correct"] and rendering["correct"] and scheduling["correct"]}
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return 0 if probe_returncode == 0 and provenance["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
