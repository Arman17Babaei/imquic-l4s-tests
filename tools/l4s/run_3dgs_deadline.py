#!/usr/bin/env python3
"""Run deadline-limited 3DGS scene transfers over IMQUIC + DualPI2."""

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
from three_dgs_bundle import (
    THREEDGS_PINNED_REVISION,
    build_scene_bundle,
    bundle_summary,
    image_psnr,
    render_bundle,
    require_dependency,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "imquic-3dgs-moq"
DEFAULT_3DGS_DIR = Path(os.environ.get("THREEDGS_DIR", ROOT / "deps" / "3dgs_over_moq"))
MODES = ("reno", "prague")


def stop(process) -> None:
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def parse_deadlines(value: str) -> list[int]:
    deadlines = []
    for item in value.split(","):
        try:
            deadline = int(item)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"invalid deadline {item!r}") from error
        if deadline <= 0:
            raise argparse.ArgumentTypeError("deadlines must be positive milliseconds")
        deadlines.append(deadline)
    if not deadlines:
        raise argparse.ArgumentTypeError("at least one deadline is required")
    return deadlines


def configure_dualpi2(switch, bottleneck: str) -> None:
    for device in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
        subprocess.run(
            ["tc", "qdisc", "del", "dev", device, "root"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["tc", "qdisc", "add", "dev", device, "root", "handle", "1:",
             "htb", "default", "1"], check=True,
        )
        subprocess.run(
            ["tc", "class", "add", "dev", device, "parent", "1:",
             "classid", "1:1", "htb", "rate", bottleneck, "burst", "32k"],
            check=True,
        )
        subprocess.run(
            ["tc", "qdisc", "add", "dev", device, "parent", "1:1",
             "handle", "10:", "dualpi2", "target", "1ms", "tupdate", "1ms"],
            check=True,
        )


def save_qdisc_stats(switch, case: Path, label: str) -> None:
    with (case / "dualpi2-stats.txt").open("a", encoding="utf-8") as stream:
        stream.write(f"snapshot={label}\n")
        for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
            stream.write(f"device={interface}\n{detailed_tc_state(interface)}")


def run_case(
    client,
    server,
    switch,
    root: Path,
    source_bundle: Path,
    source_summary: dict[str, object],
    mode: str,
    deadline_ms: int,
    repetition: int,
    args,
    reference_png: Path | None,
) -> dict[str, object]:
    case = root / f"{mode}-deadline-{deadline_ms:05d}ms-rep-{repetition:02d}"
    case.mkdir(parents=True, exist_ok=False)
    configure_dualpi2(switch, args.bottleneck)
    save_qdisc_stats(switch, case, "before")

    metadata = {
        "mode": mode,
        "deadline_ms": deadline_ms,
        "repetition": repetition,
        "bottleneck": args.bottleneck,
        "source_bundle": str(source_bundle),
        "source_objects": source_summary["objects"],
        "source_payload_bytes": source_summary["payload_bytes"],
        "frame_index": args.frame_index,
    }
    (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    captures = []
    streams = []
    publisher = subscriber = None
    try:
        for interface, name in ((f"{switch.name}-eth1", "switch-client.pcap"),
                                (f"{switch.name}-eth2", "switch-server.pcap")):
            log = (case / f"{name}.log").open("w", encoding="utf-8")
            streams.append(log)
            captures.append(subprocess.Popen(
                ["tcpdump", "-U", "-s", "128", "-i", interface, "-w", str(case / name)],
                stdout=log, stderr=subprocess.STDOUT,
            ))

        publisher_log = (case / "publisher.log").open("w", encoding="utf-8")
        subscriber_log = (case / "subscriber.log").open("w", encoding="utf-8")
        streams.extend((publisher_log, subscriber_log))
        publisher = server.popen(
            [str(BINARY), "publisher", server.IP(), "4443", mode, str(source_bundle),
             str(deadline_ms), str(case / "transport-metrics.csv"),
             str(case / "publisher-result.json")],
            cwd=str(ROOT / "deps" / "imquic" / "src"),
            stdout=publisher_log, stderr=subprocess.STDOUT,
        )
        time.sleep(0.4)
        if publisher.poll() is not None:
            raise RuntimeError(f"{case.name}: publisher exited before subscription")
        metadata["subscription_started_epoch"] = time.time()
        subscriber = client.popen(
            [str(BINARY), "subscriber", server.IP(), "4443", mode,
             str(case / "received.bundle"), str(deadline_ms), "-",
             str(case / "subscriber-result.json")],
            cwd=str(ROOT / "deps" / "imquic" / "src"),
            stdout=subscriber_log, stderr=subprocess.STDOUT,
        )
        timeout = deadline_ms / 1000.0 + 20
        subscriber_status = subscriber.wait(timeout=timeout)
        publisher_status = publisher.wait(timeout=timeout)
        metadata["finished_epoch"] = time.time()
        metadata["publisher_status"] = publisher_status
        metadata["subscriber_status"] = subscriber_status
        (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        if publisher_status or subscriber_status:
            raise RuntimeError(f"{case.name}: publisher/subscriber failed")
    finally:
        for process in (subscriber, publisher, *captures):
            stop(process)
        save_qdisc_stats(switch, case, "after")
        for stream in streams:
            stream.close()

    publisher_result = json.loads((case / "publisher-result.json").read_text(encoding="utf-8"))
    subscriber_result = json.loads((case / "subscriber-result.json").read_text(encoding="utf-8"))
    if not publisher_result.get("validated") or not subscriber_result.get("validated"):
        raise RuntimeError(f"{case.name}: endpoint validation failed")
    received_summary = bundle_summary(case / "received.bundle")
    if received_summary["objects"] != subscriber_result["received_objects"]:
        raise RuntimeError(f"{case.name}: received bundle/result object count mismatch")

    source_bytes = int(source_summary["payload_bytes"])
    source_objects = int(source_summary["objects"])
    received_bytes = int(received_summary["payload_bytes"])
    received_objects = int(received_summary["objects"])
    result: dict[str, object] = {
        "case": case.name,
        "mode": mode,
        "deadline_ms": deadline_ms,
        "repetition": repetition,
        "source_objects": source_objects,
        "source_payload_bytes": source_bytes,
        "received_objects": received_objects,
        "received_payload_bytes": received_bytes,
        "object_coverage": received_objects / source_objects if source_objects else 0.0,
        "byte_coverage": received_bytes / source_bytes if source_bytes else 0.0,
        "publisher_source_fully_queued": bool(publisher_result.get("source_fully_queued")),
        "rendered": False,
        "psnr_db": None,
    }

    if not args.no_render:
        render = render_bundle(
            case / "received.bundle", args.cache, args.trace, case / "render",
            args.three_dgs_dir, frame_index=args.frame_index, device=args.device,
            width=args.width, height=args.height,
            allow_unpinned=args.allow_unpinned_3dgs,
        )
        result["rendered"] = render["rendered"]
        result["decoded_gaussians"] = render["decoded_gaussians"]
        candidate = Path(str(render["frame_png"]))
        if reference_png is not None and candidate.is_file():
            result["psnr_db"] = image_psnr(reference_png, candidate)

    (case / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, default=DEFAULT_3DGS_DIR)
    parser.add_argument("--allow-unpinned-3dgs", action="store_true")
    parser.add_argument("--deadlines-ms", type=parse_deadlines, default=parse_deadlines("1000"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--bottleneck", default="20mbit")
    parser.add_argument("--frame-index", type=int, default=0)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("3DGS deadline experiment must run as root")
    if args.repetitions <= 0:
        parser.error("--repetitions must be positive")
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    if any(mode not in ("reno", "bbr", "prague") for mode in modes):
        parser.error("--modes may contain only reno,bbr,prague")
    for path, label in ((BINARY, "fixture binary"), (args.cache, "cache"), (args.trace, "trace")):
        if not Path(path).exists():
            raise SystemExit(f"missing {label}: {path}")

    dependency = require_dependency(
        args.three_dgs_dir, allow_unpinned=args.allow_unpinned_3dgs
    )
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "inputs"
    inputs.mkdir()
    source_bundle = inputs / "scene.bundle"
    source_summary = build_scene_bundle(
        args.cache, source_bundle, args.three_dgs_dir,
        allow_unpinned=args.allow_unpinned_3dgs,
    )
    (inputs / "scene-summary.json").write_text(
        json.dumps(source_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    reference_png = None
    if not args.no_render:
        reference = render_bundle(
            source_bundle, args.cache, args.trace, args.output / "reference-render",
            args.three_dgs_dir, frame_index=args.frame_index, device=args.device,
            width=args.width, height=args.height,
            allow_unpinned=args.allow_unpinned_3dgs,
        )
        if reference["rendered"]:
            reference_png = Path(str(reference["frame_png"]))

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    server = net.addHost("server", ip="10.0.0.2/24")
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(client, switch)
    net.addLink(switch, server)
    results = []
    try:
        net.start()
        write_experiment_record(
            args.output,
            scenario="3dgs-deadline",
            configuration={
                "deadlines_ms": args.deadlines_ms,
                "repetitions": args.repetitions,
                "modes": modes,
                "bottleneck": args.bottleneck,
                "moqt_version": 18,
                "object_delivery_timeout": "subscriber parameter equal to case deadline",
                "source_bundle": source_summary,
                "cache": {"path": str(args.cache.resolve()), "sha256": sha256_file(args.cache)},
                "trace": {"path": str(args.trace.resolve()), "sha256": sha256_file(args.trace)},
                "3dgs_dependency": dependency,
                "3dgs_pinned_revision": THREEDGS_PINNED_REVISION,
                "render": {
                    "enabled": not args.no_render,
                    "frame_index": args.frame_index,
                    "device": args.device,
                    "width": args.width,
                    "height": args.height,
                },
                "qdisc": {"root": "HTB", "child": "DualPI2", "target": "1ms", "tupdate": "1ms"},
            },
            topology={
                "nodes": {"client": "10.0.0.1/24", "server": "10.0.0.2/24", "switch": "s1 OVSBridge"},
                "links": ["client<->s1", "s1<->server"],
                "forward_direction": "server-to-client",
            },
            observed_network=capture_mininet_state(client, server, switch),
        )
        for repetition in range(1, args.repetitions + 1):
            for deadline_ms in args.deadlines_ms:
                for mode in modes:
                    results.append(run_case(
                        client, server, switch, args.output, source_bundle, source_summary,
                        mode, deadline_ms, repetition, args, reference_png,
                    ))
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)

    summary = {
        "scenario": "3dgs-deadline",
        "cases": results,
        "reference_png": str(reference_png) if reference_png is not None else None,
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
