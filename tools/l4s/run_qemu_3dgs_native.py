#!/usr/bin/env python3
"""Provision a PLY-derived GSP2 bundle and run the native 3DGS QEMU path."""
from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def wait_for_web_server(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("host SGSS static server exited during startup")
        with socket.socket() as connection:
            connection.settimeout(0.2)
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("host SGSS static server did not become ready")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path,
                        default=ROOT / "results/3dgs/media/point-cloud")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--qemu-memory", type=int, default=2048,
                        help="guest RAM in MiB (default: 2048)")
    parser.add_argument("--qemu-cpus", type=int, default=2,
                        help="guest vCPUs (default: 2)")
    parser.add_argument("--sidecar-port", type=int, default=7790)
    parser.add_argument("--web-port", type=int, default=8765)
    parser.add_argument("--no-open-browser", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("qemu_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not (args.bundle / "manifest.json").is_file():
        raise SystemExit(f"missing exported bundle: {args.bundle}; run make 3dgs-static-export")
    for label, port in (("sidecar", args.sidecar_port), ("web", args.web_port)):
        if port < 1 or port > 65535:
            raise SystemExit(f"--{label}-port must be from 1 through 65535")
    if args.qemu_memory < 1024:
        raise SystemExit("--qemu-memory must be at least 1024 MiB")
    if args.qemu_cpus < 1:
        raise SystemExit("--qemu-cpus must be positive")
    node = shutil.which("node")
    ws_module = ROOT / "deps/3dgs_over_moq/native/sgss-moq-client/node_modules/ws"
    if node is None:
        raise SystemExit("host Node.js is required to provision the QEMU sidecar")
    if not ws_module.is_dir():
        raise SystemExit(
            f"missing {ws_module}; run npm ci in native/sgss-moq-client first"
        )
    with tempfile.TemporaryDirectory(prefix="3dgs-qemu-input-") as temporary:
        archive = Path(temporary) / "point-cloud.tar.gz"
        modules_archive = Path(temporary) / "node-modules.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(args.bundle, arcname="point-cloud", recursive=True)
        with tarfile.open(modules_archive, "w:gz") as output:
            output.add(ws_module, arcname="node_modules/ws", recursive=True)
        guest_options = "--interactive" if args.interactive else ""
        command = [
            sys.executable, str(ROOT / "tools/l4s/run_qemu_timeseries_test.py"),
            "--make-target", "l4s-3dgs-native-guest-check",
            "--guest-result-name", "run",
            "--guest-result-root", "results/3dgs/qemu",
            "--destination-root", "results/3dgs/qemu",
            "--destination-prefix", "point-cloud",
            "--guest-file", f"{archive}=inputs/point-cloud.tar.gz",
            "--guest-file", f"{Path(node).resolve()}=inputs/node",
            "--guest-file", f"{modules_archive}=inputs/node-modules.tar.gz",
            "--make-variable", "THREEDGS_MEDIA_ARCHIVE=inputs/point-cloud.tar.gz",
            "--make-variable", "THREEDGS_NODE=inputs/node",
            "--make-variable", "THREEDGS_NODE_MODULES_ARCHIVE=inputs/node-modules.tar.gz",
            "--make-variable", f"THREEDGS_NATIVE_ARGS={guest_options} --duration {args.duration}",
            "--forward-sidecar-port", str(args.sidecar_port),
            "--memory", str(args.qemu_memory),
            "--cpus", str(args.qemu_cpus),
        ]
        if args.allow_dirty:
            command.append("--allow-dirty")
        command.extend(argument for argument in args.qemu_args if argument != "--")
        web = None
        try:
            if args.interactive:
                static_entry = ROOT / "deps/3dgs_over_moq/native/sgss-moq-client/static_only.mjs"
                web = subprocess.Popen(
                    [node, static_entry, f"--port={args.web_port}"], cwd=ROOT
                )
                wait_for_web_server(web, args.web_port)
                url = (
                    f"http://127.0.0.1:{args.web_port}/web_client/research/demo.html"
                    f"?sidecar=ws%3A%2F%2F127.0.0.1%3A{args.sidecar_port}"
                    "&remoteHost=10.0.0.2&remotePort=4443"
                )
                print(f"Host SGSS client: {url}", flush=True)
                print("Keep this command running while using the browser; stopping it removes the WebSocket tunnel.", flush=True)
                if not args.no_open_browser:
                    webbrowser.open(url)
            subprocess.run(command, cwd=ROOT, check=True)
        finally:
            if web is not None and web.poll() is None:
                web.terminate()
                try:
                    web.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    web.kill()
                    web.wait()


if __name__ == "__main__":
    main()
