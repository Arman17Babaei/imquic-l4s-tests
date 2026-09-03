#!/usr/bin/env python3
"""Run the native SGSS MOQT path across a DualPI2 Mininet bottleneck."""
from __future__ import annotations

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

from experiment_metadata import capture_mininet_state, detailed_tc_state, write_experiment_record
from run_3dgs_deadline import configure_dualpi2

ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = ROOT / "build/sgss-imquic-publisher"
SUBSCRIBER = ROOT / "build/sgss-imquic-subscriber"
SIDECAR = ROOT / "deps/3dgs_over_moq/native/sgss-moq-client/server.mjs"
SMOKE = ROOT / "deps/3dgs_over_moq/native/sgss-moq-client/native_qemu_smoke.mjs"


def stop(process) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bottleneck", default="100mbit")
    parser.add_argument("--htb-burst", default="256k")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("native 3DGS Mininet setup must run as root")
    if not (args.bundle / "manifest.json").is_file():
        raise SystemExit(f"missing bundle manifest: {args.bundle}")
    for path in (PUBLISHER, SUBSCRIBER, SIDECAR, SMOKE):
        if not path.exists():
            raise SystemExit(f"missing native client component: {path}")
    args.output.mkdir(parents=True, exist_ok=False)
    certificate, key = args.output / "cert.pem", args.output / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-subj", "/CN=10.0.0.2", "-keyout", key, "-out", certificate,
                    "-days", "1"], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    manifest = json.loads((args.bundle / "manifest.json").read_text(encoding="utf-8"))
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    server = net.addHost("server", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(client, switch)
    net.addLink(switch, server)
    publisher = sidecar = capture = None
    proxy = None
    streams = []
    try:
        net.start()
        subprocess.run(["modprobe", "sch_dualpi2"], check=True)
        configure_dualpi2(switch, args.bottleneck, args.htb_burst, "15ms", "16ms", "1ms")
        write_experiment_record(
            args.output, scenario="3dgs-native-qemu",
            configuration={
                "question": "Can the SGSS browser contract receive base and enhancement GSP2 objects over native imquic through DualPI2?",
                "hypothesis": "A visible base object and later enhancement object cross the emulated bottleneck without raw PLY transfer.",
                "control": "deterministic PLY-derived static bundle",
                "independent_variable": "subscriber subgroup admission and priority update",
                "dependent_variables": ["received object bytes", "Gaussian counts", "qdisc counters"],
                "inconclusive_if": "manifest, base, enhancement, packet capture, or endpoint validation is missing",
                "bundle": {"scene": manifest.get("scene"), "source": manifest.get("source"),
                           "export": manifest.get("export")},
                "moqt_version": 19, "bottleneck": args.bottleneck,
                "interactive": args.interactive,
            },
            topology={"client": "10.0.0.1/24 sidecar+subscriber",
                      "server": "10.0.0.2/24 publisher", "switch": "s1 DualPI2"},
            observed_network=capture_mininet_state(client, server, switch),
        )
        publisher_log = (args.output / "publisher.log").open("w", encoding="utf-8")
        sidecar_log = (args.output / "sidecar.log").open("w", encoding="utf-8")
        capture_log = (args.output / "tcpdump.log").open("w", encoding="utf-8")
        streams.extend((publisher_log, sidecar_log, capture_log))
        capture = subprocess.Popen(["tcpdump", "-U", "-i", f"{switch.name}-eth1",
                                    "udp", "port", "4443", "-w", str(args.output / "moqt.pcap")],
                                   stdout=capture_log, stderr=subprocess.STDOUT)
        publisher = server.popen([str(PUBLISHER), str(args.bundle), str(certificate),
                                  str(key), "10.0.0.2", "4443"],
                                 stdout=publisher_log, stderr=subprocess.STDOUT)
        environment = {**os.environ, "SGSS_IMQUIC_SUBSCRIBER": str(SUBSCRIBER)}
        sidecar = client.popen(["node", str(SIDECAR), "--native", "--host=0.0.0.0",
                                "--port=7790"], env=environment,
                               stdout=sidecar_log, stderr=subprocess.STDOUT)
        proxy = subprocess.Popen(["python3", str(ROOT / "tools/l4s/tcp_forward.py"),
                                  "--listen=0.0.0.0:7790", "--target=10.0.0.1:7790",
                                  f"--network-pid={client.pid}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if publisher.poll() is not None or sidecar.poll() is not None:
                raise RuntimeError("native publisher or sidecar exited during startup")
            if "7790" in client.cmd("ss -ltn"):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("sidecar did not listen on port 7790")
        if args.interactive:
            (args.output / "connection.json").write_text(json.dumps({
                "sidecar": "ws://10.0.0.1:7790", "publisher": "10.0.0.2:4443",
                "scene": manifest.get("scene"), "durationSeconds": args.duration,
            }, indent=2) + "\n", encoding="utf-8")
            time.sleep(args.duration)
        else:
            smoke_log = (args.output / "smoke.log").open("w", encoding="utf-8")
            streams.append(smoke_log)
            smoke = client.popen(["node", str(SMOKE),
                                  "--sidecar=ws://127.0.0.1:7790", "--remote=10.0.0.2",
                                  "--port=4443", f"--output={args.output / 'smoke-result.json'}"],
                                 stdout=smoke_log, stderr=subprocess.STDOUT)
            if smoke.wait(timeout=max(30, args.duration)) != 0:
                raise RuntimeError("native browser-contract smoke test failed")
            result = json.loads((args.output / "smoke-result.json").read_text(encoding="utf-8"))
            if not result.get("validated") or result["base"]["subgroup"] != 0 or result["enhancement"]["subgroup"] != 1:
                raise RuntimeError("native smoke result lacks base or enhancement delivery")
        (args.output / "dualpi2-stats.txt").write_text("\n".join(
            f"device={name}\n{detailed_tc_state(name)}"
            for name in (f"{switch.name}-eth1", f"{switch.name}-eth2")), encoding="utf-8")
    finally:
        for process in (proxy, sidecar, publisher, capture):
            stop(process)
        for stream in streams:
            stream.close()
        net.stop()
        certificate.unlink(missing_ok=True)
        key.unlink(missing_ok=True)
    pcap = args.output / "moqt.pcap"
    if not args.interactive and (not pcap.is_file() or pcap.stat().st_size <= 24):
        raise RuntimeError("no MOQT packets were captured across the bottleneck")
    print(f"native 3DGS QEMU guest path: PASS ({args.output})")


if __name__ == "__main__":
    main()
