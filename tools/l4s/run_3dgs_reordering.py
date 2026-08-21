#!/usr/bin/env python3
"""Run the partial-L4S post-send correction experiment for 3DGS."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import time
from itertools import zip_longest
from pathlib import Path

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSBridge

from experiment_metadata import detailed_tc_state, write_experiment_record
from reordering_workload import (
    derive_first_visible_track_order,
    manifest_importance_ranks,
    reorder_bundle_by_track_order,
    validate_admission_order,
    write_trace_release_schedule,
)
from reordering_workload_v2 import split_base_enhancement
from run_3dgs_priority_split import stop, validate_path
from split_3dgs_priority import object_identity
from three_dgs_bundle import read_bundle, sha256_file

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "imquic-3dgs-moq-scheduled"
PACKET_LOGGER = ROOT / "tools" / "l4s" / "capture_udp_order.py"
DEFAULT_3DGS = Path(os.environ.get("THREEDGS_DIR", ROOT / "deps" / "3dgs_over_moq"))

# Keep the two causal flows on the historical ports used by the packet-order
# analyzer. A third connection is used only for Enhancement that is deliberately
# moved back into L4S during the sweep.
PATHS = {
    "low-reno": {
        "role": "enhancement_classic", "mode": "reno", "port": 4443,
        "semantic_layer": "enhancement",
    },
    "high-prague": {
        "role": "base", "mode": "prague", "port": 4444,
        "semantic_layer": "base",
    },
    "enh-prague": {
        "role": "enhancement_l4s", "mode": "prague", "port": 4445,
        "semantic_layer": "enhancement",
    },
}


def _fractions(value: str) -> list[float]:
    try:
        result = [float(item) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "fractions must be comma-separated numbers in [0,1]"
        ) from error
    if not result or any(not 0.0 <= item <= 1.0 for item in result):
        raise argparse.ArgumentTypeError(
            "fractions must be comma-separated numbers in [0,1]"
        )
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


def _configure_bottlenecks(provider_egress: str, downstream_egress: str, args) -> None:
    # Reset for every case. DualPI2 controller state must not leak from one
    # fraction/repetition into the next.
    _dualpi2(provider_egress, args, rate=args.l4s_rate, burst=args.l4s_burst)
    if args.downstream_mode == "dualpi2":
        _dualpi2(
            downstream_egress, args,
            rate=args.classic_rate, burst=args.classic_burst,
        )
    else:
        _classic_fifo(downstream_egress, args)


def _disable_offloads(interfaces: list[str]) -> dict[str, str]:
    """Make packet fingerprints stable across capture points.

    QUIC UDP GSO/GRO can otherwise expose different packet boundaries on the
    three interfaces even though the network did not reorder anything.
    Unsupported ethtool features are ignored, but the final feature state is
    retained as evidence.
    """
    features = (
        "gro", "gso", "tso", "lro", "tx-udp-segmentation",
        "rx-udp-gro-forwarding",
    )
    evidence: dict[str, str] = {}
    for interface in interfaces:
        for feature in features:
            subprocess.run(
                ["ethtool", "-K", interface, feature, "off"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        state = subprocess.run(
            ["ethtool", "-k", interface], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ).stdout
        evidence[interface] = state
    return evidence


def _pcap_capture(interface: str, path: Path, *, server_ip: str):
    # Only Base/Prague (4444) and Classic Enhancement/Reno (4443) are causal
    # evidence. Enhancement/Prague (4445) is intentionally excluded.
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
    frozen["order_source"] = (
        "bicycle first-visible track order, frozen before the network run"
    )
    (inputs / "frozen-demand-order.json").write_text(
        json.dumps(frozen, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    prepared: dict[float, tuple[Path, dict[str, object]]] = {}
    for fraction in args.enhancement_l4s_fractions:
        tag = f"{fraction:.4f}".rstrip("0").rstrip(".").replace(".", "p")
        root = inputs / f"enh-l4s-{tag}"
        manifest = split_base_enhancement(
            args.source_bundle,
            root,
            enhancement_l4s_fraction=fraction,
            importance=args.importance,
        )
        importance_ranks = manifest_importance_ranks(manifest)
        path_inputs: dict[str, dict[str, object]] = {}
        for name, config in PATHS.items():
            source_bundle = Path(str(manifest[config["role"]]["path"]))
            ordered_bundle = root / f"{name}-ordered.bundle"
            ordered = reorder_bundle_by_track_order(
                source_bundle, ordered_bundle, list(frozen["track_order"])
            )
            schedule_path = root / f"{name}-release-ms.txt"
            schedule = write_trace_release_schedule(
                ordered_bundle,
                schedule_path,
                frozen_demand=frozen,
                initial_release_ms=args.initial_base_release_ms,
                time_scale=args.demand_time_scale,
                fallback_spacing_ms=args.fallback_track_spacing_ms,
                importance_ranks=importance_ranks,
            )
            path_inputs[name] = {
                "bundle": str(ordered_bundle),
                "ordered": ordered,
                "schedule": schedule,
            }
        manifest["path_inputs"] = path_inputs
        (root / "reordering-input.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        prepared[fraction] = (root, manifest)
    return prepared


def _write_combined_timeline(
    case: Path,
    path_results: dict[str, dict[str, object]],
    manifest: dict[str, object],
    *,
    workload_start_epoch_us: int,
) -> dict[str, object]:
    manifest_by_identity: dict[tuple[object, ...], dict[str, object]] = {}
    for raw in manifest["objects"]:
        row = dict(raw)
        identity = (
            row["track_id"], row["group_id"],
            int(row["subgroup_id"]), int(row["object_id"]),
        )
        manifest_by_identity[identity] = row

    rows: list[dict[str, object]] = []
    for name, result in path_results.items():
        subscriber = result["subscriber"]
        subscriber_start = int(subscriber["started_epoch_us"])
        timeline_path = case / name / "arrival-timeline.csv"
        received_path = case / name / "received.bundle"
        missing = object()
        with timeline_path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            for timeline_row, payload in zip_longest(
                reader, read_bundle(received_path), fillvalue=missing
            ):
                if timeline_row is missing or payload is missing:
                    raise RuntimeError(f"{case / name}: timeline/bundle length mismatch")
                embedded = object_identity(payload)
                identity = (
                    embedded["track_id"], embedded["group_id"],
                    int(embedded["subgroup_id"]), int(embedded["object_id"]),
                )
                source = manifest_by_identity.get(identity)
                if source is None:
                    raise RuntimeError(f"{case / name}: object absent from semantic manifest")
                if source["path"] != name:
                    raise RuntimeError(
                        f"{case / name}: object {identity} assigned to {source['path']}"
                    )
                path_time_us = int(timeline_row["arrival_time_us"])
                absolute_epoch_us = subscriber_start + path_time_us
                rows.append(
                    {
                        "experiment_time_us": absolute_epoch_us - workload_start_epoch_us,
                        "absolute_epoch_us": absolute_epoch_us,
                        "path": name,
                        "semantic_layer": source["semantic_layer"],
                        "congestion": PATHS[name]["mode"],
                        "path_arrival_time_us": path_time_us,
                        "bundle_record_index": int(timeline_row["bundle_record_index"]),
                        "source_record_index": int(source["source_record_index"]),
                        "importance_rank": int(source["importance_rank"]),
                        "payload_bytes": int(timeline_row["payload_bytes"]),
                        "num_gaussians": int(timeline_row["num_gaussians"]),
                        "track_id": identity[0],
                        "group_id": identity[1],
                        "subgroup_id": int(identity[2]),
                        "object_id": int(identity[3]),
                    }
                )
    rows.sort(
        key=lambda row: (
            int(row["absolute_epoch_us"]), str(row["path"]),
            int(row["bundle_record_index"]),
        )
    )
    destination = case / "combined-arrival-timeline.csv"
    fields = (
        "experiment_time_us", "absolute_epoch_us", "path", "semantic_layer",
        "congestion", "path_arrival_time_us", "bundle_record_index",
        "source_record_index", "importance_rank", "payload_bytes",
        "num_gaussians", "track_id", "group_id", "subgroup_id", "object_id",
    )
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "origin_epoch_us": workload_start_epoch_us,
        "objects": len(rows),
    }


def _wait_publishers_ready(
    ready_paths: dict[str, Path], publishers: dict[str, object], timeout_s: float
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for name, process in publishers.items():
            if process.poll() is not None:
                raise RuntimeError(f"{name} publisher exited before workload gate")
        if all(path.is_file() for path in ready_paths.values()):
            return
        time.sleep(0.005)
    missing = [name for name, path in ready_paths.items() if not path.is_file()]
    raise RuntimeError(f"publishers not ready before timeout: {', '.join(missing)}")


def _run_case(
    *, client, server, background_sink,
    provider_egress: str, downstream_egress: str,
    provider_ingress: str,
    experiment_root: Path, split_root: Path, manifest: dict[str, object],
    fraction: float, repetition: int, args,
) -> dict[str, object]:
    tag = f"{fraction:.4f}".rstrip("0").rstrip(".").replace(".", "p")
    case = experiment_root / f"l4s-enh-{tag}-rep-{repetition:02d}"
    case.mkdir()

    _configure_bottlenecks(provider_egress, downstream_egress, args)
    time.sleep(0.05)

    def save_tc_state(label: str) -> None:
        (case / f"tc-state-{label}.txt").write_text(
            "provider_egress\n"
            f"{detailed_tc_state(provider_egress)}"
            "downstream_egress\n"
            f"{detailed_tc_state(downstream_egress)}",
            encoding="utf-8",
        )

    save_tc_state("before")

    active_paths = [
        name for name, config in PATHS.items()
        if int(manifest[config["role"]]["objects"]) > 0
    ]
    if "high-prague" not in active_paths:
        raise RuntimeError("Base/Prague path must always be active")
    if len(active_paths) < 2:
        raise RuntimeError("experiment requires a separate Enhancement connection")
    if args.application_queue_budget_bytes < len(active_paths) * 65536:
        raise RuntimeError(
            "application queue budget is too small for the active connections"
        )
    per_path_queue_budget = args.application_queue_budget_bytes // len(active_paths)

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
    ready_paths: dict[str, Path] = {}
    go_path = case / "workload-go.txt"
    workload_start_epoch_us = None
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
            duration = (
                args.dc_background_warmup_s
                + args.deadline_ms / 1000.0
                + args.subscriber_guard_ms / 1000.0
                + 2
            )
            bg_client = server.popen(
                ["iperf3", "-c", background_sink.IP(), "-p", "5201",
                 "-t", f"{duration:g}", "-b", f"{args.dc_background_mbps}M",
                 "-C", args.dc_background_cc, "--json"],
                stdout=bg_client_log, stderr=subprocess.STDOUT,
            )
            processes.append(bg_client)
            time.sleep(args.dc_background_warmup_s)

        for name in active_paths:
            config = PATHS[name]
            path_root = case / name
            path_root.mkdir()
            pub_log = (path_root / "publisher.log").open("w")
            sub_log = (path_root / "subscriber.log").open("w")
            streams += [pub_log, sub_log]
            ready_path = path_root / "publisher-ready"
            ready_paths[name] = ready_path
            path_input = manifest["path_inputs"][name]
            publishers[name] = server.popen(
                [
                    str(BINARY), "publisher-scheduled-gated", server.IP(),
                    str(config["port"]), config["mode"],
                    str(path_input["bundle"]), str(args.deadline_ms),
                    str(path_root / "transport-metrics.csv"),
                    str(path_root / "publisher-result.json"),
                    str(path_input["schedule"]["path"]),
                    str(path_root / "admission-order.csv"),
                    str(ready_path), str(go_path), str(per_path_queue_budget),
                ],
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=pub_log, stderr=subprocess.STDOUT,
            )
            processes.append(publishers[name])

        time.sleep(0.3)
        launch_order = sorted(active_paths)
        if repetition % 2 == 0:
            launch_order.reverse()
        subscriber_deadline_ms = args.deadline_ms + args.subscriber_guard_ms
        for name in launch_order:
            config = PATHS[name]
            path_root = case / name
            subscribers[name] = client.popen(
                [
                    str(BINARY), "subscriber", server.IP(), str(config["port"]),
                    config["mode"], str(path_root / "received.bundle"),
                    str(subscriber_deadline_ms),
                    str(path_root / "arrival-timeline.csv"),
                    str(path_root / "subscriber-result.json"),
                ],
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=next(
                    stream for stream in streams
                    if getattr(stream, "name", "") == str(path_root / "subscriber.log")
                ),
                stderr=subprocess.STDOUT,
            )
            processes.append(subscribers[name])

        _wait_publishers_ready(
            ready_paths, publishers, args.endpoint_ready_timeout_s
        )
        workload_start_epoch_us = (
            time.time_ns() // 1000 + int(args.workload_start_lead_ms * 1000)
        )
        go_path.write_text(f"{workload_start_epoch_us}\n", encoding="utf-8")

        timeout = (
            args.deadline_ms + args.subscriber_guard_ms
        ) / 1000.0 + 30
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
        for label, process in captures.items():
            _stop_capture(process, label)
        for stream in streams:
            stream.close()
        save_tc_state("after")

    if workload_start_epoch_us is None:
        raise RuntimeError(f"{case.name}: workload gate was never released")

    subscriber_deadline_ms = args.deadline_ms + args.subscriber_guard_ms
    path_results = {
        name: validate_path(case / name, subscriber_deadline_ms)
        for name in active_paths
    }
    admission_results = {
        name: validate_admission_order(
            Path(str(manifest["path_inputs"][name]["schedule"]["path"])),
            case / name / "admission-order.csv",
        )
        for name in active_paths
    }

    publisher_starts = {
        name: int(path_results[name]["publisher"]["publisher_started_epoch_us"])
        for name in active_paths
    }
    requested_starts = {
        name: int(path_results[name]["publisher"]["workload_start_epoch_us"])
        for name in active_paths
    }
    if any(value != workload_start_epoch_us for value in requested_starts.values()):
        raise RuntimeError(f"{case.name}: publishers did not consume the same workload gate")
    publisher_start_skew_us = max(publisher_starts.values()) - min(publisher_starts.values())
    publisher_start_lateness_us = max(
        abs(value - workload_start_epoch_us) for value in publisher_starts.values()
    )
    if publisher_start_skew_us > int(args.max_start_skew_ms * 1000):
        raise RuntimeError(
            f"{case.name}: workload start skew {publisher_start_skew_us / 1000:.3f} ms "
            f"exceeds {args.max_start_skew_ms:.3f} ms"
        )
    if publisher_start_lateness_us > int(args.max_start_lateness_ms * 1000):
        raise RuntimeError(
            f"{case.name}: workload gate lateness {publisher_start_lateness_us / 1000:.3f} ms "
            f"exceeds {args.max_start_lateness_ms:.3f} ms"
        )

    combined = _write_combined_timeline(
        case, path_results, manifest,
        workload_start_epoch_us=workload_start_epoch_us,
    )
    balance = manifest["balance"]
    result = {
        "case": case.name,
        "repetition": repetition,
        "downstream_mode": args.downstream_mode,
        "requested_enhancement_l4s_fraction": fraction,
        "actual_enhancement_l4s_fraction": balance["actual_enhancement_l4s_fraction"],
        "actual_total_l4s_byte_fraction": balance["actual_total_l4s_byte_fraction"],
        # Compatibility field consumed by the existing packet-order analyzer.
        "requested_l4s_byte_fraction": balance["actual_total_l4s_byte_fraction"],
        "base_byte_fraction": balance["base_byte_fraction"],
        "workload_start_epoch_us": workload_start_epoch_us,
        "publisher_start_epoch_us": publisher_starts,
        "publisher_start_skew_us": publisher_start_skew_us,
        "publisher_start_lateness_us": publisher_start_lateness_us,
        "application_queue_budget_bytes": args.application_queue_budget_bytes,
        "per_path_queue_budget_bytes": per_path_queue_budget,
        "active_paths": active_paths,
        "combined_timeline": combined,
        "paths": {
            name: {
                "active": name in active_paths,
                "semantic_layer": PATHS[name]["semantic_layer"],
                "congestion": PATHS[name]["mode"],
                "port": PATHS[name]["port"],
                "assigned_objects": int(manifest[PATHS[name]["role"]]["objects"]),
                "assigned_payload_bytes": int(
                    manifest[PATHS[name]["role"]]["payload_bytes"]
                ),
                "received_objects": (
                    path_results[name]["bundle"]["objects"] if name in path_results else 0
                ),
                "received_payload_bytes": (
                    path_results[name]["bundle"]["payload_bytes"]
                    if name in path_results else 0
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
    parser.add_argument(
        "--importance", choices=("native-tier", "opacity", "scale", "opacity-scale"),
        default="native-tier",
    )
    parser.add_argument(
        "--enhancement-l4s-fractions", "--l4s-fractions",
        dest="enhancement_l4s_fractions", type=_fractions,
        default=_fractions("0,0.5,1"),
        help=(
            "fraction of Enhancement payload placed on a second Prague connection; "
            "Base is always Prague (deprecated alias: --l4s-fractions)"
        ),
    )
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--initial-base-release-ms", type=float, default=5.0)
    parser.add_argument("--demand-time-scale", type=float, default=1.0)
    parser.add_argument("--fallback-track-spacing-ms", type=float, default=100.0)
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument("--subscriber-guard-ms", type=int, default=3000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--l4s-rate", default="300mbit")
    parser.add_argument("--l4s-burst", default="512k")
    parser.add_argument("--classic-rate", default="50mbit")
    parser.add_argument("--classic-burst", default="128k")
    parser.add_argument(
        "--classic-buffer-packets", type=int, default=128,
        help="finite downstream FIFO; 128 MTU packets is about 31 ms at 50 Mbit/s",
    )
    parser.add_argument(
        "--downstream-mode", choices=("classic", "dualpi2"), default="classic",
        help="client-facing qdisc on the second switch",
    )
    parser.add_argument("--dualpi2-target", default="15ms")
    parser.add_argument("--dualpi2-tupdate", default="16ms")
    parser.add_argument("--dualpi2-step", default="1ms")
    parser.add_argument("--dc-background-mbps", type=float, default=280.0)
    parser.add_argument("--dc-background-cc", choices=("reno", "cubic"), default="reno")
    parser.add_argument("--dc-background-warmup-s", type=float, default=2.0)
    parser.add_argument(
        "--application-queue-budget-bytes", type=int, default=4 * 1024 * 1024,
        help="aggregate sender-side transport queue budget, divided across active media paths",
    )
    parser.add_argument("--endpoint-ready-timeout-s", type=float, default=10.0)
    parser.add_argument("--workload-start-lead-ms", type=float, default=200.0)
    parser.add_argument("--max-start-skew-ms", type=float, default=2.0)
    parser.add_argument("--max-start-lateness-ms", type=float, default=5.0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--capture-mode", choices=("packet-log", "pcap", "none"),
        default="packet-log",
        help="packet-order evidence for Base/Prague versus Classic Enhancement/Reno",
    )
    parser.add_argument("--no-pcap", action="store_true")
    args = parser.parse_args()

    if args.no_pcap:
        args.capture_mode = "none"
    if os.geteuid() != 0:
        raise SystemExit("run as root")
    if not BINARY.is_file():
        raise SystemExit(
            "build scheduled fixture with: sh tools/l4s/build_3dgs_scheduled_fixture.sh"
        )
    if args.capture_mode == "pcap" and shutil.which("tcpdump") is None:
        raise SystemExit("tcpdump is required for --capture-mode pcap")
    if args.capture_mode != "none" and shutil.which("ethtool") is None:
        raise SystemExit("ethtool is required for capture-safe offload control")
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
    if args.subscriber_guard_ms <= 0 or args.application_queue_budget_bytes <= 0:
        parser.error("subscriber guard and application queue budget must be positive")
    if args.endpoint_ready_timeout_s <= 0 or args.workload_start_lead_ms < 0:
        parser.error("invalid endpoint synchronization timing")
    if args.max_start_skew_ms < 0 or args.max_start_lateness_ms < 0:
        parser.error("start validation thresholds must be non-negative")

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

        server_egress = _interface(server, provider)
        provider_ingress = _interface(provider, server)
        provider_egress = _interface(provider, downstream)
        downstream_ingress = _interface(downstream, provider)
        downstream_egress = _interface(downstream, client)
        client_link = _interface(client, downstream)
        offload_evidence = {}
        if args.capture_mode != "none":
            offload_evidence = _disable_offloads(
                [
                    server_egress, provider_ingress, provider_egress,
                    downstream_ingress, downstream_egress, client_link,
                ]
            )

        _configure_bottlenecks(provider_egress, downstream_egress, args)
        write_experiment_record(
            args.output,
            scenario="3dgs-partial-l4s-post-send-reordering-v2",
            configuration={
                "question": (
                    "Can Base generated at a later bicycle-track demand event overtake "
                    "already-admitted Enhancement in the provider DualQ, and can that "
                    "ordering improve frame SSIM after a slower Classic bottleneck?"
                ),
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
                "semantic_split": (
                    "subgroup 0 Base always on Prague; subgroups 1/2 Enhancement "
                    "swept between a second Prague connection and Reno"
                ),
                "enhancement_l4s_fractions": args.enhancement_l4s_fractions,
                "importance": args.importance,
                "application_admission_policy": (
                    "lowest global importance rank among currently eligible objects, per path"
                ),
                "viewport_eligibility": (
                    "all objects of a track become eligible at that track's frozen "
                    "first-visible bicycle timestamp; later Base can therefore arrive "
                    "while earlier-track Enhancement is already in flight"
                ),
                "application_queue_budget_bytes": args.application_queue_budget_bytes,
                "publisher_gate": {
                    "ready_timeout_s": args.endpoint_ready_timeout_s,
                    "start_lead_ms": args.workload_start_lead_ms,
                    "max_start_skew_ms": args.max_start_skew_ms,
                    "max_start_lateness_ms": args.max_start_lateness_ms,
                },
                "dc_background_mbps": args.dc_background_mbps,
                "dc_background_cc": args.dc_background_cc,
                "provider_dualpi2_rate": args.l4s_rate,
                "downstream_rate": args.classic_rate,
                "downstream_mode": args.downstream_mode,
                "classic_buffer_packets": args.classic_buffer_packets,
                "dualpi2_target": args.dualpi2_target,
                "dualpi2_tupdate": args.dualpi2_tupdate,
                "dualpi2_step": args.dualpi2_step,
                "fixed_wire_delay_ms": 0.0,
                "processing_delay": (
                    "not separately emulated; constant processing would cancel across cases; "
                    "serialization is imposed by the configured HTB rates"
                ),
                "deadline_ms": args.deadline_ms,
                "subscriber_guard_ms": args.subscriber_guard_ms,
                "repetitions": args.repetitions,
                "packet_order_capture": args.capture_mode,
                "packet_order_scope": (
                    "Base/Prague port 4444 versus Classic Enhancement/Reno port 4443; "
                    "Enhancement/Prague port 4445 is excluded from inversion counting"
                ),
                "offload_control": offload_evidence,
            },
            topology={
                "forward": (
                    "server -> s1(DualPI2) -> s2(DualPI2) -> client"
                    if args.downstream_mode == "dualpi2"
                    else "server -> s1(DualPI2) -> s2(FIFO) -> client"
                ),
                "aggregation_background": (
                    "server -> s1 -> s2 -> background_sink; shares provider egress "
                    "but not the client-facing downstream qdisc"
                ),
                "media_paths": PATHS,
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

        for fraction in args.enhancement_l4s_fractions:
            split_root, manifest = prepared[fraction]
            for repetition in range(1, args.repetitions + 1):
                results.append(
                    _run_case(
                        client=client, server=server,
                        background_sink=background_sink,
                        provider_egress=provider_egress,
                        downstream_egress=downstream_egress,
                        provider_ingress=provider_ingress,
                        experiment_root=args.output,
                        split_root=split_root,
                        manifest=manifest,
                        fraction=fraction,
                        repetition=repetition,
                        args=args,
                    )
                )
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)

    (args.output / "summary.json").write_text(
        json.dumps(
            {
                "scenario": "3dgs-partial-l4s-post-send-reordering-v2",
                "frozen_demand_order": str(
                    args.output / "inputs" / "frozen-demand-order.json"
                ),
                "cases": results,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
