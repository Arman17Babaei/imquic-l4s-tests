#!/usr/bin/env python3
"""Run the partial-L4S post-send reordering experiment for 3DGS."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSBridge

from experiment_metadata import detailed_tc_state, write_experiment_record
from reordering_workload import (
    derive_first_visible_track_order,
    reorder_bundle_by_track_order,
    split_by_l4s_fraction,
    write_immediate_release_schedule,
    write_trace_release_schedule,
)
from run_3dgs_priority_split import PATHS, stop, validate_path, write_combined_timeline
from three_dgs_bundle import sha256_file

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "imquic-3dgs-moq-scheduled"
DEFAULT_3DGS = Path(os.environ.get("THREEDGS_DIR", ROOT / "deps" / "3dgs_over_moq"))


def _fractions(value: str) -> list[float]:
    result = [float(item) for item in value.split(",") if item.strip()]
    if not result or any(not 0.0 < item < 1.0 for item in result):
        raise argparse.ArgumentTypeError("fractions must be comma-separated values in (0,1)")
    return result


def _interface(left, right) -> str:
    links = left.connectionsTo(right)
    if len(links) != 1:
        raise RuntimeError(f"expected one link between {left.name} and {right.name}")
    return str(links[0][0])


def _reset(interface: str) -> None:
    subprocess.run(
        ["tc", "qdisc", "del", "dev", interface, "root"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _dualpi2(interface: str, args) -> None:
    _reset(interface)
    subprocess.run(
        ["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:",
         "htb", "default", "1"], check=True,
    )
    subprocess.run(
        ["tc", "class", "add", "dev", interface, "parent", "1:",
         "classid", "1:1", "htb", "rate", args.l4s_rate,
         "burst", args.l4s_burst, "cburst", args.l4s_burst], check=True,
    )
    subprocess.run(
        ["tc", "qdisc", "add", "dev", interface, "parent", "1:1",
         "handle", "10:", "dualpi2", "target", args.dualpi2_target,
         "tupdate", args.dualpi2_tupdate, "step_thresh", args.dualpi2_step],
        check=True,
    )


def _classic_fifo(interface: str, args) -> None:
    _reset(interface)
    subprocess.run(
        ["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:",
         "htb", "default", "1"], check=True,
    )
    subprocess.run(
        ["tc", "class", "add", "dev", interface, "parent", "1:",
         "classid", "1:1", "htb", "rate", args.classic_rate,
         "burst", args.classic_burst, "cburst", args.classic_burst], check=True,
    )
    subprocess.run(
        ["tc", "qdisc", "add", "dev", interface, "parent", "1:1",
         "handle", "20:", "pfifo", "limit", str(args.classic_buffer_packets)],
        check=True,
    )


def _capture(interface: str, path: Path):
    return subprocess.Popen(
        ["tcpdump", "-i", interface, "-s", "0", "-U", "-w", str(path), "udp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def _stop_capture(process, label: str) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    stderr = process.stderr.read() if process.stderr else ""
    if process.returncode not in (0, 130, -signal.SIGINT):
        raise RuntimeError(f"tcpdump {label} failed: {stderr}")


def _prepare_inputs(args) -> dict[float, tuple[Path, dict[str, object]]]:
    inputs = args.output / "inputs"
    inputs.mkdir()

    frozen = derive_first_visible_track_order(
        args.cache, args.trace, args.three_dgs_dir,
        width=args.width, height=args.height, frame_stride=args.frame_stride,
        allow_unpinned=args.allow_unpinned_3dgs,
    )
    frozen["initial_base_release_ms"] = args.initial_base_release_ms
    frozen["demand_time_scale"] = args.demand_time_scale
    frozen["order_source"] = "bicycle trace first-visible tracks, frozen before network run"
    (inputs / "frozen-demand-order.json").write_text(
        json.dumps(frozen, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    prepared = {}
    for fraction in args.l4s_fractions:
        tag = f"{fraction:.4f}".rstrip("0").rstrip(".").replace(".", "p")
        root = inputs / f"l4s-{tag}"
        split = split_by_l4s_fraction(
            args.source_bundle, root, l4s_fraction=fraction, importance=args.importance
        )
        ordered = root / "high-priority-ordered.bundle"
        split["ordered_high"] = reorder_bundle_by_track_order(
            root / "high-priority.bundle", ordered, list(frozen["track_order"])
        )
        split["high_release_schedule"] = write_trace_release_schedule(
            ordered, root / "high-release-ms.txt", frozen_demand=frozen,
            initial_release_ms=args.initial_base_release_ms,
            time_scale=args.demand_time_scale,
            fallback_spacing_ms=args.fallback_track_spacing_ms,
        )
        split["low_release_schedule"] = write_immediate_release_schedule(
            root / "low-priority.bundle", root / "low-release-ms.txt"
        )
        (root / "reordering-input.json").write_text(
            json.dumps(split, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        prepared[fraction] = (root, split)
    return prepared


def _run_case(
    *, client, server, background_sink, provider, downstream,
    provider_ingress: str, provider_egress: str, downstream_egress: str,
    experiment_root: Path, split_root: Path, manifest: dict[str, object],
    fraction: float, repetition: int, args,
) -> dict[str, object]:
    tag = f"{fraction:.4f}".rstrip("0").rstrip(".").replace(".", "p")
    case = experiment_root / f"l4s-{tag}-rep-{repetition:02d}"
    case.mkdir()

    captures = {}
    if not args.no_pcap:
        for label, interface in {
            "l4s_ingress": provider_ingress,
            "l4s_egress": provider_egress,
            "classic_egress": downstream_egress,
        }.items():
            captures[label] = _capture(interface, case / f"{label}.pcap")
        time.sleep(0.2)

    processes = []
    streams = []
    publishers = {}
    subscribers = {}
    try:
        if args.dc_background_mbps > 0:
            server.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
            background_sink.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
            bg_server_log = (case / "background-server.json").open("w")
            bg_client_log = (case / "background-client.json").open("w")
            streams += [bg_server_log, bg_client_log]
            bg_server = background_sink.popen(
                ["iperf3", "-s", "-1", "-p", "5201", "--json"],
                stdout=bg_server_log, stderr=subprocess.STDOUT,
            )
            processes.append(bg_server)
            time.sleep(0.2)
            duration = args.dc_background_warmup_s + args.deadline_ms / 1000 + 2
            bg_client = server.popen(
                ["iperf3", "-c", background_sink.IP(), "-p", "5201",
                 "-t", f"{duration:g}", "-b", f"{args.dc_background_mbps}M",
                 "-C", args.dc_background_cc, "--json"],
                stdout=bg_client_log, stderr=subprocess.STDOUT,
            )
            processes.append(bg_client)
            time.sleep(args.dc_background_warmup_s)

        bundles = {
            "high-prague": split_root / "high-priority-ordered.bundle",
            "low-reno": split_root / "low-priority.bundle",
        }
        schedules = {
            "high-prague": split_root / "high-release-ms.txt",
            "low-reno": split_root / "low-release-ms.txt",
        }
        subscriber_logs = {}
        for name, config in PATHS.items():
            path_root = case / name
            path_root.mkdir()
            pub_log = (path_root / "publisher.log").open("w")
            sub_log = (path_root / "subscriber.log").open("w")
            streams += [pub_log, sub_log]
            subscriber_logs[name] = sub_log
            publishers[name] = server.popen(
                [str(BINARY), "publisher-scheduled", server.IP(), str(config["port"]),
                 config["mode"], str(bundles[name]), str(args.deadline_ms),
                 str(path_root / "transport-metrics.csv"),
                 str(path_root / "publisher-result.json"), str(schedules[name])],
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=pub_log, stderr=subprocess.STDOUT,
            )
            processes.append(publishers[name])

        time.sleep(0.4)
        launch_order = (
            ["high-prague", "low-reno"] if repetition % 2
            else ["low-reno", "high-prague"]
        )
        for name in launch_order:
            config = PATHS[name]
            path_root = case / name
            subscribers[name] = client.popen(
                [str(BINARY), "subscriber", server.IP(), str(config["port"]),
                 config["mode"], str(path_root / "received.bundle"),
                 str(args.deadline_ms), str(path_root / "arrival-timeline.csv"),
                 str(path_root / "subscriber-result.json")],
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=subscriber_logs[name], stderr=subprocess.STDOUT,
            )
            processes.append(subscribers[name])

        timeout = args.deadline_ms / 1000 + 30
        status = {
            f"{name}-subscriber": process.wait(timeout=timeout)
            for name, process in subscribers.items()
        }
        status.update({
            f"{name}-publisher": process.wait(timeout=timeout)
            for name, process in publishers.items()
        })
        if any(status.values()):
            raise RuntimeError(f"{case.name}: endpoint failure: {status}")
    finally:
        for process in reversed(processes):
            stop(process)
        for process_label, process in captures.items():
            _stop_capture(process, process_label)
        for stream in streams:
            stream.close()

    path_results = {
        name: validate_path(case / name, args.deadline_ms)
        for name in PATHS
    }
    combined = write_combined_timeline(case, path_results, manifest)
    result = {
        "case": case.name,
        "repetition": repetition,
        "requested_l4s_byte_fraction": fraction,
        "actual_l4s_byte_fraction": manifest["balance"]["actual_l4s_byte_fraction"],
        "combined_timeline": combined,
        "paths": {
            name: {
                "congestion": PATHS[name]["mode"],
                "received_objects": path_results[name]["bundle"]["objects"],
                "received_payload_bytes": path_results[name]["bundle"]["payload_bytes"],
                "publisher_started_epoch_us": path_results[name]["publisher"].get(
                    "publisher_started_epoch_us"
                ),
            }
            for name in PATHS
        },
    }
    (case / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, default=DEFAULT_3DGS)
    parser.add_argument("--allow-unpinned-3dgs", action="store_true")
    parser.add_argument("--importance", choices=("native-tier", "opacity", "scale", "opacity-scale"), default="native-tier")
    parser.add_argument("--l4s-fractions", type=_fractions, default=_fractions("0.10,0.25,0.50"))
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--initial-base-release-ms", type=float, default=5.0)
    parser.add_argument("--demand-time-scale", type=float, default=1.0)
    parser.add_argument("--fallback-track-spacing-ms", type=float, default=100.0)
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--l4s-rate", default="300mbit")
    parser.add_argument("--l4s-burst", default="512k")
    parser.add_argument("--classic-rate", default="50mbit")
    parser.add_argument("--classic-burst", default="128k")
    parser.add_argument("--classic-buffer-packets", type=int, default=1000)
    parser.add_argument("--dualpi2-target", default="15ms")
    parser.add_argument("--dualpi2-tupdate", default="16ms")
    parser.add_argument("--dualpi2-step", default="1ms")
    parser.add_argument("--dc-background-mbps", type=float, default=280.0)
    parser.add_argument("--dc-background-cc", choices=("reno", "cubic"), default="reno")
    parser.add_argument("--dc-background-warmup-s", type=float, default=2.0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--no-pcap", action="store_true")
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("run as root")
    if not BINARY.is_file():
        raise SystemExit("build scheduled fixture with: sh tools/l4s/build_3dgs_scheduled_fixture.sh")
    if not args.no_pcap and shutil.which("tcpdump") is None:
        raise SystemExit("tcpdump is required unless --no-pcap is used")
    if args.dc_background_mbps > 0 and shutil.which("iperf3") is None:
        raise SystemExit("iperf3 is required for datacenter aggregation background")
    for path in (args.source_bundle, args.cache, args.trace, args.three_dgs_dir):
        if not path.exists():
            raise SystemExit(f"missing required path: {path}")
    if args.frame_stride <= 0 or args.demand_time_scale <= 0:
        parser.error("frame stride and demand time scale must be positive")

    args.output.mkdir(parents=True, exist_ok=False)
    prepared = _prepare_inputs(args)

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    server = net.addHost("server", ip="10.0.0.1/24")
    client = net.addHost("client", ip="10.0.0.2/24")
    background_sink = net.addHost("background_sink", ip="10.0.0.3/24")
    provider = net.addSwitch("s_l4s", failMode="standalone")
    downstream = net.addSwitch("s_classic", failMode="standalone")
    net.addLink(server, provider)
    net.addLink(provider, downstream)
    net.addLink(downstream, client)
    net.addLink(downstream, background_sink)

    results = []
    try:
        net.start()
        subprocess.run(["modprobe", "sch_dualpi2"], check=True)
        provider_ingress = _interface(provider, server)
        provider_egress = _interface(provider, downstream)
        downstream_egress = _interface(downstream, client)
        _dualpi2(provider_egress, args)
        _classic_fifo(downstream_egress, args)

        write_experiment_record(
            args.output,
            scenario="3dgs-partial-l4s-post-send-reordering",
            configuration={
                "source_bundle": str(args.source_bundle.resolve()),
                "source_sha256": sha256_file(args.source_bundle),
                "trace": str(args.trace.resolve()),
                "trace_sha256": sha256_file(args.trace),
                "l4s_fractions": args.l4s_fractions,
                "importance": args.importance,
                "demand_time_scale": args.demand_time_scale,
                "dc_background_mbps": args.dc_background_mbps,
                "dc_background_cc": args.dc_background_cc,
                "provider_dualpi2_rate": args.l4s_rate,
                "downstream_classic_rate": args.classic_rate,
                "classic_buffer_packets": args.classic_buffer_packets,
                "dualpi2_target": args.dualpi2_target,
                "dualpi2_tupdate": args.dualpi2_tupdate,
                "dualpi2_step": args.dualpi2_step,
            },
            topology={
                "forward": "server -> s_l4s(DualPI2) -> s_classic(FIFO) -> client",
                "aggregation_background": (
                    "server -> s_l4s -> s_classic -> background_sink; "
                    "shares provider egress, not client FIFO"
                ),
                "interfaces": {
                    "provider_ingress": provider_ingress,
                    "provider_egress": provider_egress,
                    "downstream_egress": downstream_egress,
                },
            },
            observed_network={
                "provider_egress_tc": detailed_tc_state(provider_egress),
                "downstream_egress_tc": detailed_tc_state(downstream_egress),
            },
        )

        for fraction in args.l4s_fractions:
            split_root, manifest = prepared[fraction]
            for repetition in range(1, args.repetitions + 1):
                results.append(_run_case(
                    client=client, server=server, background_sink=background_sink,
                    provider=provider, downstream=downstream,
                    provider_ingress=provider_ingress,
                    provider_egress=provider_egress,
                    downstream_egress=downstream_egress,
                    experiment_root=args.output, split_root=split_root,
                    manifest=manifest, fraction=fraction, repetition=repetition,
                    args=args,
                ))
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)

    (args.output / "summary.json").write_text(
        json.dumps({
            "scenario": "3dgs-partial-l4s-post-send-reordering",
            "frozen_demand_order": str(args.output / "inputs" / "frozen-demand-order.json"),
            "cases": results,
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
