#!/usr/bin/env python3
"""Run deadline-limited 3DGS scene transfers over IMQUIC + DualPI2."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import time
from pathlib import Path

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSBridge

from experiment_metadata import capture_mininet_state, detailed_tc_state, write_experiment_record
from object_timeline import (
    render_arrival_comparison,
    summarize_timeline,
    timeline_series,
    transport_metric_series,
)
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
BACKGROUND_CONTROLLERS = ("reno", "cubic", "bbr", "bbr2")
BACKGROUND_MODULES = {"bbr": "tcp_bbr", "bbr2": "tcp_bbr2"}


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


def configure_dualpi2(
    switch,
    bottleneck: str,
    htb_burst: str,
    target: str,
    tupdate: str,
    step_thresh: str,
) -> None:
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
             "classid", "1:1", "htb", "rate", bottleneck,
             "burst", htb_burst, "cburst", htb_burst],
            check=True,
        )
        subprocess.run(
            ["tc", "qdisc", "add", "dev", device, "parent", "1:1",
             "handle", "10:", "dualpi2", "target", target,
             "tupdate", tupdate, "step_thresh", step_thresh],
            check=True,
        )


def save_qdisc_stats(switch, case: Path, label: str) -> None:
    with (case / "dualpi2-stats.txt").open("a", encoding="utf-8") as stream:
        stream.write(f"snapshot={label}\n")
        for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
            stream.write(f"device={interface}\n{detailed_tc_state(interface)}")


def configure_background_tcp(client, server) -> None:
    client.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
    server.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
    server.cmd(
        "iptables -t mangle -A OUTPUT -p tcp --dport 5201 "
        "-j TOS --set-tos 0x00"
    )
    client.cmd(
        "iptables -t mangle -A OUTPUT -p tcp --sport 5201 "
        "-j TOS --set-tos 0x00"
    )


def interface_counters(interface: str) -> dict[str, object]:
    output = subprocess.run(
        ["ip", "-s", "-j", "link", "show", "dev", interface],
        check=True, text=True, stdout=subprocess.PIPE,
    ).stdout
    rows = json.loads(output)
    if len(rows) != 1:
        raise RuntimeError(f"could not read counters for {interface}")
    return rows[0]


def iperf_summary(path: Path, expected_congestion: str) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    congestion = data.get("end", {}).get("sender_tcp_congestion")
    if congestion != expected_congestion:
        raise RuntimeError(
            f"{path}: background sender congestion is {congestion!r}, "
            f"expected {expected_congestion!r}"
        )
    received = data.get("end", {}).get("sum_received", {})
    return {
        "congestion_control": congestion,
        "received_mbps": float(received.get("bits_per_second", 0)) / 1_000_000,
        "received_bytes": int(received.get("bytes", 0)),
        "seconds": float(received.get("seconds", 0)),
    }


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
    configure_dualpi2(
        switch, args.bottleneck, args.htb_burst, args.dualpi2_target,
        args.dualpi2_tupdate, args.dualpi2_step_thresh,
    )
    save_qdisc_stats(switch, case, "before")
    counter_before = {
        interface: interface_counters(interface)
        for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2")
    }

    metadata = {
        "mode": mode,
        "deadline_ms": deadline_ms,
        "repetition": repetition,
        "bottleneck": args.bottleneck,
        "htb_burst": args.htb_burst,
        "dualpi2_target": args.dualpi2_target,
        "dualpi2_tupdate": args.dualpi2_tupdate,
        "dualpi2_step_thresh": args.dualpi2_step_thresh,
        "source_bundle": str(source_bundle),
        "source_objects": source_summary["objects"],
        "source_payload_bytes": source_summary["payload_bytes"],
        "frame_index": args.frame_index,
        "background_mbps": args.background_mbps,
        "background_congestion": args.background_congestion,
        "background_ecn": "not-ect",
        "background_warmup_seconds": args.background_warmup_seconds,
    }
    (case / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    streams = []
    publisher = subscriber = background_server = background_client = None
    try:
        if args.background_mbps > 0:
            background_server_log = (case / "iperf-server.json").open(
                "w", encoding="utf-8"
            )
            background_client_log = (case / "iperf-client.json").open(
                "w", encoding="utf-8"
            )
            streams.extend((background_server_log, background_client_log))
            background_server = client.popen(
                ["iperf3", "-s", "-1", "-p", "5201", "--json"],
                stdout=background_server_log, stderr=subprocess.STDOUT,
            )
            time.sleep(0.3)
            background_seconds = (
                args.background_warmup_seconds + deadline_ms / 1000.0 + 1
            )
            background_client = server.popen(
                ["iperf3", "-c", client.IP(), "-p", "5201", "-t",
                 f"{background_seconds:g}", "-b", f"{args.background_mbps}M",
                 "-C", args.background_congestion, "--json"],
                stdout=background_client_log, stderr=subprocess.STDOUT,
            )
            metadata["background_started_epoch"] = time.time()
            time.sleep(args.background_warmup_seconds)

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
             str(case / "received.bundle"), str(deadline_ms),
             str(case / "arrival-timeline.csv"),
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
        if background_client is not None and background_client.wait(timeout=15) != 0:
            raise RuntimeError(f"{case.name}: BBRv2 background client failed")
        if background_server is not None and background_server.wait(timeout=10) != 0:
            raise RuntimeError(f"{case.name}: BBRv2 background server failed")
        metadata["background_finished_epoch"] = time.time()
        (case / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        for process in (subscriber, publisher, background_client, background_server):
            stop(process)
        save_qdisc_stats(switch, case, "after")
        counter_after = {
            interface: interface_counters(interface)
            for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2")
        }
        (case / "interface-counters.json").write_text(
            json.dumps({"before": counter_before, "after": counter_after},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for stream in streams:
            stream.close()

    publisher_result = json.loads((case / "publisher-result.json").read_text(encoding="utf-8"))
    subscriber_result = json.loads((case / "subscriber-result.json").read_text(encoding="utf-8"))
    if not publisher_result.get("validated") or not subscriber_result.get("validated"):
        raise RuntimeError(f"{case.name}: endpoint validation failed")
    received_summary = bundle_summary(case / "received.bundle")
    timeline_summary = summarize_timeline(case / "arrival-timeline.csv")
    if received_summary["objects"] != subscriber_result["received_objects"]:
        raise RuntimeError(f"{case.name}: received bundle/result object count mismatch")
    if timeline_summary["objects"] != received_summary["objects"]:
        raise RuntimeError(f"{case.name}: arrival timeline/bundle object count mismatch")
    if timeline_summary["payload_bytes"] != received_summary["payload_bytes"]:
        raise RuntimeError(f"{case.name}: arrival timeline/bundle byte count mismatch")
    if timeline_summary["gaussians"] != subscriber_result.get("received_gaussians"):
        raise RuntimeError(f"{case.name}: arrival timeline/result Gaussian count mismatch")
    if timeline_summary["first_arrival_us"] != subscriber_result.get("first_object_time_us"):
        raise RuntimeError(f"{case.name}: first object timestamp mismatch")
    if timeline_summary["last_arrival_us"] != subscriber_result.get("last_object_time_us"):
        raise RuntimeError(f"{case.name}: last object timestamp mismatch")
    last_arrival_us = timeline_summary["last_arrival_us"]
    if last_arrival_us is not None and int(last_arrival_us) > deadline_ms * 1000:
        raise RuntimeError(f"{case.name}: arrival timeline contains post-deadline object")

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
        "received_gaussians": int(timeline_summary["gaussians"]),
        "first_object_time_us": timeline_summary["first_arrival_us"],
        "last_object_time_us": timeline_summary["last_arrival_us"],
        "arrival_timeline": timeline_summary,
        "bundle_finalized_time_us": subscriber_result.get("bundle_finalized_time_us"),
        "subscriber_started_epoch_us": subscriber_result.get("started_epoch_us"),
        "timeline_origin": subscriber_result.get("timeline_origin"),
        "object_coverage": received_objects / source_objects if source_objects else 0.0,
        "byte_coverage": received_bytes / source_bytes if source_bytes else 0.0,
        "publisher_source_fully_queued": bool(publisher_result.get("source_fully_queued")),
        "rendered": False,
        "psnr_db": None,
        "background": (
            {
                "offered_mbps": args.background_mbps,
                **iperf_summary(
                    case / "iperf-client.json", args.background_congestion
                ),
            }
            if args.background_mbps > 0 else {
                "offered_mbps": 0.0,
                "congestion_control": None,
                "received_mbps": 0.0,
                "received_bytes": 0,
                "seconds": 0.0,
            }
        ),
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cache", type=Path)
    source.add_argument("--source-bundle", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, default=DEFAULT_3DGS_DIR)
    parser.add_argument("--allow-unpinned-3dgs", action="store_true")
    parser.add_argument("--deadlines-ms", type=parse_deadlines, default=parse_deadlines("1000"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--bottleneck", default="20mbit")
    parser.add_argument("--htb-burst", default="32k")
    parser.add_argument("--dualpi2-target", default="15ms")
    parser.add_argument("--dualpi2-tupdate", default="16ms")
    parser.add_argument("--dualpi2-step-thresh", default="1ms")
    parser.add_argument("--frame-index", type=int, default=0)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--background-mbps", type=float, default=0)
    parser.add_argument(
        "--background-congestion", choices=BACKGROUND_CONTROLLERS, default="bbr2"
    )
    parser.add_argument("--background-warmup-seconds", type=float, default=2)
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("3DGS deadline experiment must run as root")
    if args.repetitions <= 0:
        parser.error("--repetitions must be positive")
    if args.background_mbps < 0:
        parser.error("--background-mbps must be non-negative")
    if args.background_warmup_seconds < 0:
        parser.error("--background-warmup-seconds must be non-negative")
    if not args.no_render and (args.cache is None or args.trace is None):
        parser.error("rendering requires --cache and --trace")
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    if any(mode not in ("reno", "bbr", "prague") for mode in modes):
        parser.error("--modes may contain only reno,bbr,prague")
    required_paths = [(BINARY, "fixture binary")]
    if args.cache is not None:
        required_paths.append((args.cache, "cache"))
    if args.source_bundle is not None:
        required_paths.append((args.source_bundle, "source bundle"))
    if args.trace is not None:
        required_paths.append((args.trace, "trace"))
    for path, label in required_paths:
        if not Path(path).exists():
            raise SystemExit(f"missing {label}: {path}")
    dependency = None
    if args.cache is not None or not args.no_render:
        dependency = require_dependency(
            args.three_dgs_dir, allow_unpinned=args.allow_unpinned_3dgs
        )
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "inputs"
    inputs.mkdir()
    if args.source_bundle is not None:
        source_bundle = args.source_bundle.resolve()
        source_summary = bundle_summary(args.source_bundle)
        source_summary["source"] = str(args.source_bundle.resolve())
    else:
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
        subprocess.run(["modprobe", "sch_dualpi2"], check=True)
        module = BACKGROUND_MODULES.get(args.background_congestion)
        if args.background_mbps > 0 and module is not None:
            subprocess.run(["modprobe", module], check=True)
        configure_background_tcp(client, server)
        if args.background_mbps > 0:
            available = server.cmd(
                "sysctl -n net.ipv4.tcp_available_congestion_control"
            ).split()
            if args.background_congestion not in available:
                raise RuntimeError(
                    f"TCP {args.background_congestion} unavailable: {' '.join(available)}"
                )
        write_experiment_record(
            args.output,
            scenario="3dgs-deadline",
            configuration={
                "question": (
                    "How do Reno/Not-ECT and Prague/ECT(1) differ in 3DGS "
                    "splat arrival rate while sharing one DualPI2 bottleneck "
                    "with the same paced BBRv2 background flow?"
                ),
                "hypothesis": (
                    "Under the identical shared load, both transfers remain "
                    "active, while Prague exposes ECT(1)/CE controller response "
                    "and a measurably different cumulative and one-second splat "
                    "arrival trajectory from Reno."
                ),
                "control": "Reno/Not-ECT with identical scene and background TCP",
                "independent_variable": "MoQ congestion controller (reno or prague)",
                "dependent_variables": [
                    "cumulative received splats",
                    "one-second splat arrival rate",
                    "received bytes and objects",
                    "transport RTT/cwnd/ECN metrics",
                    "background TCP achieved throughput",
                ],
                "inconclusive_if": (
                    "either endpoint or BBRv2 flow fails, timelines are invalid, "
                    "or either MoQ transfer receives no objects"
                ),
                "deadlines_ms": args.deadlines_ms,
                "repetitions": args.repetitions,
                "modes": modes,
                "bottleneck": args.bottleneck,
                "htb_burst": args.htb_burst,
                "background": {
                    "transport": "TCP iperf3",
                    "congestion_control": args.background_congestion,
                    "offered_mbps": args.background_mbps,
                    "ecn": "disabled and TOS forced to Not-ECT",
                    "warmup_seconds": args.background_warmup_seconds,
                },
                "packet_capture": "disabled",
                "moqt_version": 18,
                "object_delivery_timeout": "subscriber parameter equal to case deadline",
                "arrival_timeline": {
                    "artifact": "arrival-timeline.csv per case",
                    "clock": "subscriber monotonic clock",
                    "origin": "subscriber endpoint start",
                    "granularity": "one row per complete received MoQ object",
                },
                "source_bundle": source_summary,
                "cache": ({"path": str(args.cache.resolve()), "sha256": sha256_file(args.cache)}
                          if args.cache is not None else None),
                "trace": ({"path": str(args.trace.resolve()), "sha256": sha256_file(args.trace)}
                          if args.trace is not None else None),
                "3dgs_dependency": dependency,
                "3dgs_pinned_revision": THREEDGS_PINNED_REVISION,
                "render": {
                    "enabled": not args.no_render,
                    "frame_index": args.frame_index,
                    "device": args.device,
                    "width": args.width,
                    "height": args.height,
                },
                "qdisc": {
                    "root": "HTB",
                    "burst": args.htb_burst,
                    "cburst": args.htb_burst,
                    "child": "DualPI2",
                    "target": args.dualpi2_target,
                    "tupdate": args.dualpi2_tupdate,
                    "step_thresh": args.dualpi2_step_thresh,
                    "classic_protection": "kernel default (observed in tc evidence)",
                },
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
    comparison_timelines = {
        mode: args.output / f"{mode}-deadline-{args.deadlines_ms[0]:05d}ms-rep-01"
        / "arrival-timeline.csv"
        for mode in modes
    }
    comparison_metrics = {
        mode: args.output / f"{mode}-deadline-{args.deadlines_ms[0]:05d}ms-rep-01"
        / "transport-metrics.csv"
        for mode in modes
    }
    render_arrival_comparison(
        comparison_timelines,
        args.output / "splat-arrivals.svg",
        duration_us=args.deadlines_ms[0] * 1000,
        transport_metrics=comparison_metrics,
    )
    summary["splat_arrival_plot"] = "splat-arrivals.svg"
    summary["splat_arrival_plot_metrics"] = {
        "foreground_throughput": "received application payload bytes in one-second bins",
        "queue_delay": "smoothed RTT minus the minimum smoothed RTT observed in that run",
        "queue_delay_caveat": "RTT-derived estimate, not direct qdisc sojourn time",
    }
    summary["splat_arrival_comparison"] = {}
    for mode, timeline in comparison_timelines.items():
        arrival = timeline_series(timeline)
        transport = transport_metric_series(comparison_metrics[mode])
        queue_delays = sorted(
            delay for _, delay in transport["queue_delay_ms"]
        )
        p95_index = math.ceil(0.95 * len(queue_delays)) - 1
        last_arrival_us = int(arrival["cumulative"][-1][0])
        summary["splat_arrival_comparison"][mode] = {
            "received_splats": int(arrival["total_splats"]),
            "mean_splats_per_second": (
                int(arrival["total_splats"]) / (args.deadlines_ms[0] / 1000)
            ),
            "peak_one_second_splats": max(arrival["bins"], default=0),
            "mean_until_last_arrival_foreground_payload_mbps": (
                int(arrival["total_payload_bytes"]) * 8
                / (last_arrival_us / 1_000_000)
                / 1_000_000
            ),
            "peak_one_second_foreground_payload_mbps": (
                max(arrival["byte_bins"], default=0) * 8 / 1_000_000
            ),
            "minimum_smoothed_rtt_ms": int(transport["minimum_rtt_us"]) / 1000,
            "mean_estimated_queue_delay_ms": (
                sum(queue_delays) / len(queue_delays)
            ),
            "p95_estimated_queue_delay_ms": queue_delays[p95_index],
        }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
