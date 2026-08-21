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
    manifest_importance_ranks,
    reorder_bundle_by_track_order,
    split_by_l4s_fraction,
    validate_admission_order,
    write_trace_release_schedule,
)
from run_3dgs_priority_split import PATHS, stop, validate_path, write_combined_timeline
from three_dgs_bundle import sha256_file

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "imquic-3dgs-moq-scheduled"
PACKET_LOGGER = ROOT / "tools" / "l4s" / "capture_udp_order.py"
DEFAULT_3DGS = Path(os.environ.get("THREEDGS_DIR", ROOT / "deps" / "3dgs_over_moq"))


def _fractions(value: str) -> list[float]:
    result = [float(item) for item in value.split(",") if item.strip()]
    if not result or any(not 0.0 <= item <= 1.0 for item in result):
        raise argparse.ArgumentTypeError("fractions must be comma-separated values in [0,1]")
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


def _dualpi2(interface: str, args, *, rate: str, burst: str) -> None:
    _reset(interface)
    subprocess.run(
        ["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:",
         "htb", "default", "1"], check=True,
    )
    subprocess.run(
        ["tc", "class", "add", "dev", interface, "parent", "1:",
         "classid", "1:1", "htb", "rate", rate,
         "burst", burst, "cburst", burst], check=True,
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


def _pcap_capture(interface: str, path: Path, *, server_ip: str):
    return subprocess.Popen(
        [
            "tcpdump", "-i", interface, "-s", "0", "-U", "-w", str(path),
            "src", "host", server_ip, "and", "udp", "and", "(",
            "src", "port", "4443", "or", "src", "port", "4444", ")",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def _packet_log_capture(interface: str, path: Path, *, server_ip: str):
    return subprocess.Popen(
        [
            "python3", str(PACKET_LOGGER), "--interface", interface,
            "--output", str(path), "--stats", str(path.with_suffix(".stats.json")),
            "--server-ip", server_ip,
        ],
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
        raise RuntimeError(f"packet capture {label} failed: {stderr}")


def _prepare_inputs(args) -> dict[float, tuple[Path, dict[str, object]]]:
    inputs = args.output / "inputs"
    inputs.mkdir()

    if args.frozen_demand is not None:
        frozen = json.loads(args.frozen_demand.read_text(encoding="utf-8"))
        if not isinstance(frozen.get("track_order"), list) or not frozen["track_order"]:
            raise RuntimeError(f"{args.frozen_demand}: invalid frozen track order")
        if not isinstance(frozen.get("events"), list):
            raise RuntimeError(f"{args.frozen_demand}: invalid frozen demand events")
        frozen["frozen_demand_source"] = str(args.frozen_demand.resolve())
        frozen["frozen_demand_source_sha256"] = sha256_file(args.frozen_demand)
    else:
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
        importance_ranks = manifest_importance_ranks(split)
        ordered = root / "high-priority-ordered.bundle"
        split["ordered_high"] = reorder_bundle_by_track_order(
            root / "high-priority.bundle", ordered, list(frozen["track_order"])
        )
        ordered_low = root / "low-priority-ordered.bundle"
        split["ordered_low"] = reorder_bundle_by_track_order(
            root / "low-priority.bundle", ordered_low, list(frozen["track_order"])
        )
        split["high_release_schedule"] = write_trace_release_schedule(
            ordered, root / "high-release-ms.txt", frozen_demand=frozen,
            initial_release_ms=args.initial_base_release_ms,
            time_scale=args.demand_time_scale,
            fallback_spacing_ms=args.fallback_track_spacing_ms,
            importance_ranks=importance_ranks,
        )
        split["low_release_schedule"] = write_trace_release_schedule(
            ordered_low, root / "low-release-ms.txt", frozen_demand=frozen,
            initial_release_ms=args.initial_base_release_ms,
            time_scale=args.demand_time_scale,
            fallback_spacing_ms=args.fallback_track_spacing_ms,
            importance_ranks=importance_ranks,
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

    def save_tc_state(label: str) -> None:
        (case / f"tc-state-{label}.txt").write_text(
            "provider_egress\n"
            f"{detailed_tc_state(provider_egress)}"
            "downstream_egress\n"
            f"{detailed_tc_state(downstream_egress)}",
            encoding="utf-8",
        )

    save_tc_state("before")

    active_paths = []
    if int(manifest["low"]["objects"]) > 0:
        active_paths.append("low-reno")
    if int(manifest["high"]["objects"]) > 0:
        active_paths.append("high-prague")
    if not active_paths:
        raise RuntimeError(f"{case.name}: split has no active transport path")

    captures = {}
    if args.capture_mode != "none":
        for label, interface in {
            "provider_ingress": provider_ingress,
            "provider_egress": provider_egress,
            "downstream_egress": downstream_egress,
        }.items():
            if args.capture_mode == "pcap":
                captures[label] = _pcap_capture(
                    interface, case / f"{label}.pcap", server_ip=server.IP()
                )
            else:
                captures[label] = _packet_log_capture(
                    interface, case / f"{label}.packet-order.csv",
                    server_ip=server.IP(),
                )
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
            "low-reno": split_root / "low-priority-ordered.bundle",
        }
        schedules = {
            "high-prague": split_root / "high-release-ms.txt",
            "low-reno": split_root / "low-release-ms.txt",
        }
        subscriber_logs = {}
        for name in active_paths:
            config = PATHS[name]
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
                 str(path_root / "publisher-result.json"), str(schedules[name]),
                 str(path_root / "admission-order.csv")],
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=pub_log, stderr=subprocess.STDOUT,
            )
            processes.append(publishers[name])

        time.sleep(0.4)
        preferred_launch_order = (
            ["high-prague", "low-reno"] if repetition % 2
            else ["low-reno", "high-prague"]
        )
        launch_order = [
            name for name in preferred_launch_order if name in active_paths
        ]
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
        save_tc_state("after")

    path_results = {
        name: validate_path(case / name, args.deadline_ms)
        for name in active_paths
    }
    admission_results = {
        name: validate_admission_order(
            schedules[name], case / name / "admission-order.csv"
        )
        for name in active_paths
    }
    combined = write_combined_timeline(case, path_results, manifest)
    publisher_starts = {
        name: int(path_results[name]["publisher"]["publisher_started_epoch_us"])
        for name in active_paths
    }
    publisher_start_skew_us = max(publisher_starts.values()) - min(
        publisher_starts.values()
    )
    if publisher_start_skew_us > int(args.max_start_skew_ms * 1000):
        raise RuntimeError(
            f"{case.name}: publisher workload start skew "
            f"{publisher_start_skew_us / 1000:.3f} ms exceeds "
            f"{args.max_start_skew_ms:.3f} ms"
        )
    result = {
        "case": case.name,
        "repetition": repetition,
        "downstream_mode": args.downstream_mode,
        "requested_l4s_byte_fraction": fraction,
        "actual_l4s_byte_fraction": manifest["balance"]["actual_l4s_byte_fraction"],
        "publisher_start_epoch_us": publisher_starts,
        "publisher_start_skew_us": publisher_start_skew_us,
        "active_paths": active_paths,
        "combined_timeline": combined,
        "paths": {
            name: {
                "active": name in active_paths,
                "congestion": PATHS[name]["mode"],
                "assigned_objects": int(
                    manifest[PATHS[name]["priority"]]["objects"]
                ),
                "received_objects": (
                    path_results[name]["bundle"]["objects"]
                    if name in path_results else 0
                ),
                "received_payload_bytes": (
                    path_results[name]["bundle"]["payload_bytes"]
                    if name in path_results else 0
                ),
                "publisher_started_epoch_us": (
                    path_results[name]["publisher"].get("publisher_started_epoch_us")
                    if name in path_results else None
                ),
                "admission_order": (
                    str(case / name / "admission-order.csv")
                    if name in path_results else None
                ),
                "admission_validation": admission_results.get(name),
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
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument(
        "--frozen-demand", type=Path,
        help="reuse a previously derived frozen-demand-order.json",
    )
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
    parser.add_argument(
        "--downstream-mode", choices=("classic", "dualpi2"), default="classic",
        help="qdisc on the second switch's client-facing bottleneck",
    )
    parser.add_argument("--dualpi2-target", default="15ms")
    parser.add_argument("--dualpi2-tupdate", default="16ms")
    parser.add_argument("--dualpi2-step", default="1ms")
    parser.add_argument("--dc-background-mbps", type=float, default=280.0)
    parser.add_argument("--dc-background-cc", choices=("reno", "cubic"), default="reno")
    parser.add_argument("--dc-background-warmup-s", type=float, default=2.0)
    parser.add_argument("--max-start-skew-ms", type=float, default=50.0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--capture-mode", choices=("packet-log", "pcap", "none"),
        default="packet-log",
        help="packet-order evidence format; packet-log stores no packet bodies",
    )
    parser.add_argument(
        "--no-pcap", action="store_true",
        help="deprecated alias for --capture-mode none",
    )
    args = parser.parse_args()

    if args.no_pcap:
        args.capture_mode = "none"

    if os.geteuid() != 0:
        raise SystemExit("run as root")
    if not BINARY.is_file():
        raise SystemExit("build scheduled fixture with: sh tools/l4s/build_3dgs_scheduled_fixture.sh")
    if args.capture_mode == "pcap" and shutil.which("tcpdump") is None:
        raise SystemExit("tcpdump is required for --capture-mode pcap")
    if args.dc_background_mbps > 0 and shutil.which("iperf3") is None:
        raise SystemExit("iperf3 is required for datacenter aggregation background")
    if args.frozen_demand is None and (args.cache is None or args.trace is None):
        parser.error("--cache and --trace are required unless --frozen-demand is used")
    required_paths = [args.source_bundle]
    if args.frozen_demand is not None:
        required_paths.append(args.frozen_demand)
    else:
        required_paths.extend((args.cache, args.trace, args.three_dgs_dir))
    for path in required_paths:
        if not path.exists():
            raise SystemExit(f"missing required path: {path}")
    if args.frame_stride <= 0 or args.demand_time_scale <= 0:
        parser.error("frame stride and demand time scale must be positive")
    if args.repetitions <= 0 or args.deadline_ms <= 0:
        parser.error("repetitions and deadline must be positive")
    if args.max_start_skew_ms < 0:
        parser.error("--max-start-skew-ms must be non-negative")

    args.output.mkdir(parents=True, exist_ok=False)
    prepared = _prepare_inputs(args)
    frozen = json.loads(
        (args.output / "inputs" / "frozen-demand-order.json").read_text(
            encoding="utf-8"
        )
    )

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    server = net.addHost("server", ip="10.0.0.1/24")
    client = net.addHost("client", ip="10.0.0.2/24")
    background_sink = net.addHost("bg_sink", ip="10.0.0.3/24")
    # Mininet derives an OVS datapath ID from canonical s<number> names.
    provider = net.addSwitch("s1", failMode="standalone")
    downstream = net.addSwitch("s2", failMode="standalone")
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
        _dualpi2(
            provider_egress, args, rate=args.l4s_rate, burst=args.l4s_burst
        )
        if args.downstream_mode == "dualpi2":
            _dualpi2(
                downstream_egress, args,
                rate=args.classic_rate, burst=args.classic_burst,
            )
        else:
            _classic_fifo(downstream_egress, args)

        write_experiment_record(
            args.output,
            scenario="3dgs-partial-l4s-post-send-reordering",
            configuration={
                "source_bundle": str(args.source_bundle.resolve()),
                "source_sha256": sha256_file(args.source_bundle),
                "trace": (
                    str(args.trace.resolve()) if args.trace is not None
                    else frozen.get("trace")
                ),
                "trace_sha256": (
                    sha256_file(args.trace) if args.trace is not None
                    else frozen.get("trace_sha256")
                ),
                "frozen_demand_sha256": sha256_file(
                    args.output / "inputs" / "frozen-demand-order.json"
                ),
                "l4s_fractions": args.l4s_fractions,
                "importance": args.importance,
                "application_admission_policy": (
                    "lowest global importance rank among currently eligible objects"
                ),
                "viewport_eligibility": (
                    "Base and Enhancement objects at frozen first-visible "
                    "track time"
                ),
                "demand_time_scale": args.demand_time_scale,
                "dc_background_mbps": args.dc_background_mbps,
                "dc_background_cc": args.dc_background_cc,
                "provider_dualpi2_rate": args.l4s_rate,
                "downstream_classic_rate": args.classic_rate,
                "downstream_mode": args.downstream_mode,
                "classic_buffer_packets": args.classic_buffer_packets,
                "dualpi2_target": args.dualpi2_target,
                "dualpi2_tupdate": args.dualpi2_tupdate,
                "dualpi2_step": args.dualpi2_step,
                "deadline_ms": args.deadline_ms,
                "repetitions": args.repetitions,
                "max_start_skew_ms": args.max_start_skew_ms,
                "packet_capture": args.capture_mode == "pcap",
                "packet_order_capture": args.capture_mode,
            },
            topology={
                "forward": (
                    "server -> s1(DualPI2) -> s2(DualPI2) -> client"
                    if args.downstream_mode == "dualpi2"
                    else "server -> s1(DualPI2) -> s2(FIFO) -> client"
                ),
                "aggregation_background": (
                    "server -> s1 -> s2 -> background_sink; shares provider "
                    "egress, not the client-facing downstream qdisc"
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
