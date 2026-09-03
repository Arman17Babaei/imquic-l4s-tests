#!/usr/bin/env python3
"""Run the two-connection partial-L4S post-send reordering experiment for 3DGS."""

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
    derive_first_visible_object_order,
    manifest_importance_ranks,
    object_release_times,
    reorder_bundle_by_object_order,
    validate_admission_order,
    validate_cross_path_admission_order,
    write_trace_release_schedule,
)
from reordering_workload_v2 import split_l4s_spectrum
from run_3dgs_priority_split import stop, validate_path
from split_3dgs_priority import object_identity
from three_dgs_bundle import read_bundle, sha256_file
from run_qemu_3dgs_reordering import (
    _background_rate,
    _iperf_background_command,
)

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "imquic-3dgs-moq-scheduled"
PACKET_LOGGER = ROOT / "tools" / "l4s" / "capture_udp_order.py"
PRAGUE_WARMUP_DRAIN_GUARD_MS = 5000
DEFAULT_3DGS = Path(
    os.environ.get("THREEDGS_DIR", ROOT / "deps" / "3dgs_over_moq")
)

PATHS = {
    "low-reno": {"mode": "reno", "port": 4443, "manifest_key": "reno"},
    "high-prague": {"mode": "prague", "port": 4444, "manifest_key": "prague"},
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


def _background_enabled(args) -> bool:
    return args.dc_background_mbps is None or args.dc_background_mbps > 0


def _read_background_result(path: Path) -> dict[str, object]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid iperf background result: {path}") from error
    end = result.get("end", {})
    sent = end.get("sum_sent", {})
    received = end.get("sum_received", {})
    bytes_sent = int(sent.get("bytes", 0))
    bytes_received = int(received.get("bytes", 0))
    if max(bytes_sent, bytes_received) <= 0:
        raise RuntimeError(f"iperf background transferred no bytes: {path}")
    return {
        "bytes_sent": bytes_sent,
        "bytes_received": bytes_received,
        "bits_per_second": float(
            sent.get("bits_per_second", received.get("bits_per_second", 0.0))
        ),
        "start": result.get("start", {}),
        "end": result.get("end", {}),
        "validated": True,
    }


def _interface(left, right) -> str:
    links = left.connectionsTo(right)
    if len(links) != 1:
        raise RuntimeError(f"expected one link between {left.name} and {right.name}")
    return str(links[0][0])


def _reset(interface: str) -> None:
    subprocess.run(
        ["tc", "qdisc", "del", "dev", interface, "root"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dualpi2(interface: str, args, *, rate: str, burst: str) -> None:
    _reset(interface)
    subprocess.run(
        ["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:", "htb", "default", "1"],
        check=True,
    )
    subprocess.run(
        [
            "tc", "class", "add", "dev", interface, "parent", "1:",
            "classid", "1:1", "htb", "rate", rate,
            "burst", burst, "cburst", burst,
        ],
        check=True,
    )
    subprocess.run(
        [
            "tc", "qdisc", "add", "dev", interface, "parent", "1:1",
            "handle", "10:", "dualpi2",
            "target", args.dualpi2_target,
            "tupdate", args.dualpi2_tupdate,
            "step_thresh", args.dualpi2_step,
        ],
        check=True,
    )


def _classic_fifo(interface: str, args) -> None:
    _reset(interface)
    subprocess.run(
        ["tc", "qdisc", "add", "dev", interface, "root", "handle", "1:", "htb", "default", "1"],
        check=True,
    )
    subprocess.run(
        [
            "tc", "class", "add", "dev", interface, "parent", "1:",
            "classid", "1:1", "htb", "rate", args.classic_rate,
            "burst", args.classic_burst, "cburst", args.classic_burst,
        ],
        check=True,
    )
    subprocess.run(
        [
            "tc", "qdisc", "add", "dev", interface, "parent", "1:1",
            "handle", "20:", "pfifo", "limit", str(args.classic_buffer_packets),
        ],
        check=True,
    )


def _run_in_node(node, command: list[str], *, check: bool) -> None:
    """Run a command in a Mininet node's network namespace."""
    process = node.popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()
    if check and process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )


def _configure_fixed_delay(node, interface: str, one_way_ms: float) -> None:
    """Install propagation delay on an endpoint interface in its namespace."""
    _run_in_node(
        node,
        ["tc", "qdisc", "del", "dev", interface, "root"],
        check=False,
    )
    if one_way_ms <= 0:
        return
    _run_in_node(
        node,
        [
            "tc", "qdisc", "add", "dev", interface, "root", "netem",
            "delay", f"{one_way_ms:g}ms", "limit", "100000",
        ],
        check=True,
    )


def _configure_bottlenecks(provider_egress: str, downstream_egress: str, args) -> None:
    if args.switch_topology == "server-switch":
        _dualpi2(provider_egress, args, rate=args.isolated_switch_rate, burst=args.l4s_burst)
        return
    if args.switch_topology == "client-switch":
        if args.downstream_mode == "dualpi2":
            _dualpi2(downstream_egress, args, rate=args.classic_rate, burst=args.classic_burst)
        else:
            _classic_fifo(downstream_egress, args)
        return
    _dualpi2(provider_egress, args, rate=args.l4s_rate, burst=args.l4s_burst)
    if args.downstream_mode == "dualpi2":
        _dualpi2(downstream_egress, args, rate=args.classic_rate, burst=args.classic_burst)
    else:
        _classic_fifo(downstream_egress, args)


def _disable_offloads(interfaces: list[str]) -> dict[str, str]:
    features = (
        "gro", "gso", "tso", "lro", "tx-udp-segmentation",
        "rx-udp-gro-forwarding",
    )
    evidence: dict[str, str] = {}
    for interface in interfaces:
        for feature in features:
            subprocess.run(
                ["ethtool", "-K", interface, feature, "off"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        evidence[interface] = subprocess.run(
            ["ethtool", "-k", interface],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
    return evidence


def _pcap_capture(interface: str, path: Path, *, server_ip: str):
    return subprocess.Popen(
        [
            "tcpdump", "-i", interface, "-s", "0", "-U", "-w", str(path),
            "src", "host", server_ip, "and", "udp", "and", "(",
            "src", "port", "4443", "or", "src", "port", "4444", ")",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _packet_log_capture(interface: str, path: Path, *, server_ip: str):
    return subprocess.Popen(
        [
            "python3", str(PACKET_LOGGER),
            "--interface", interface,
            "--output", str(path),
            "--stats", str(path.with_suffix(".stats.json")),
            "--server-ip", server_ip,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
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
        if frozen.get("schema_version") != 2 or frozen.get("eligibility_granularity") != "object-aabb":
            raise RuntimeError(f"{args.frozen_demand}: expected object-aabb frozen demand schema v2")
        if not isinstance(frozen.get("object_order"), list) or not frozen["object_order"]:
            raise RuntimeError(f"{args.frozen_demand}: invalid frozen object order")
        if not isinstance(frozen.get("events"), list):
            raise RuntimeError(f"{args.frozen_demand}: invalid frozen demand events")
        frozen["frozen_demand_source"] = str(args.frozen_demand.resolve())
        frozen["frozen_demand_source_sha256"] = sha256_file(args.frozen_demand)
    else:
        frozen = derive_first_visible_object_order(
            args.source_bundle,
            args.trace,
            args.three_dgs_dir,
            width=args.width,
            height=args.height,
            frame_stride=args.frame_stride,
            allow_unpinned=args.allow_unpinned_3dgs,
        )
    frozen["initial_base_release_ms"] = args.initial_release_ms
    frozen["demand_time_scale"] = args.demand_time_scale
    frozen["initial_visibility_spread_ms"] = args.initial_visibility_spread_ms
    frozen["order_source"] = "bicycle first-visible object AABB order, frozen before network run"
    visible_keys = {tuple(event["object_key"]) for event in frozen["events"]}
    release_times = object_release_times(
        frozen,
        initial_release_ms=args.initial_release_ms,
        time_scale=args.demand_time_scale,
        initial_visibility_spread_ms=args.initial_visibility_spread_ms,
    )
    (inputs / "frozen-demand-order.json").write_text(
        json.dumps(frozen, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    prepared: dict[float, tuple[Path, dict[str, object]]] = {}
    for fraction in args.l4s_fractions:
        tag = f"{fraction:.4f}".rstrip("0").rstrip(".").replace(".", "p")
        root = inputs / f"l4s-{tag}"
        manifest = split_l4s_spectrum(
            args.source_bundle,
            root,
            l4s_fraction=fraction,
            eligible_object_keys=visible_keys,
        )
        importance_ranks = manifest_importance_ranks(manifest)
        path_inputs: dict[str, dict[str, object]] = {}
        for name, config in PATHS.items():
            source_bundle = Path(str(manifest[config["manifest_key"]]["path"]))
            ordered_bundle = root / f"{name}-ordered.bundle"
            ordered = reorder_bundle_by_object_order(source_bundle, ordered_bundle, release_times)
            schedule_path = root / f"{name}-release-ms.txt"
            schedule = write_trace_release_schedule(
                ordered_bundle,
                schedule_path,
                frozen_demand=frozen,
                initial_release_ms=args.initial_release_ms,
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
        subscriber_start = int(result["subscriber"]["started_epoch_us"])
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
                    raise RuntimeError(f"{case / name}: object absent from spectrum manifest")
                if source["path"] != name:
                    raise RuntimeError(
                        f"{case / name}: object {identity} assigned to {source['path']}"
                    )
                path_time_us = int(timeline_row["arrival_time_us"])
                absolute_epoch_us = subscriber_start + path_time_us
                rows.append({
                    "experiment_time_us": absolute_epoch_us - workload_start_epoch_us,
                    "absolute_epoch_us": absolute_epoch_us,
                    "path": name,
                    "congestion": PATHS[name]["mode"],
                    "path_arrival_time_us": path_time_us,
                    "bundle_record_index": int(timeline_row["bundle_record_index"]),
                    "source_record_index": int(source["source_record_index"]),
                    "importance_rank": int(source["importance_rank"]),
                    "layer": int(source["layer"]),
                    "mean_opacity": float(source["mean_opacity"]),
                    "payload_bytes": int(timeline_row["payload_bytes"]),
                    "num_gaussians": int(timeline_row["num_gaussians"]),
                    "track_id": identity[0],
                    "group_id": identity[1],
                    "subgroup_id": int(identity[2]),
                    "object_id": int(identity[3]),
                })

    rows.sort(
        key=lambda row: (
            int(row["absolute_epoch_us"]), str(row["path"]),
            int(row["bundle_record_index"]),
        )
    )
    destination = case / "combined-arrival-timeline.csv"
    fields = (
        "experiment_time_us", "absolute_epoch_us", "path", "congestion",
        "path_arrival_time_us", "bundle_record_index", "source_record_index",
        "importance_rank", "layer", "mean_opacity", "payload_bytes",
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


def _wait_publisher_ready(
    ready_path: Path, publisher, timeout_s: float
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if publisher.poll() is not None:
            raise RuntimeError("dual publisher exited before workload gate")
        if ready_path.is_file():
            return
        time.sleep(0.005)
    raise RuntimeError("dual publisher not ready before timeout")


def _empty_admission_validation(schedule_path: Path, admission_path: Path):
    if schedule_path.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"{schedule_path}: expected empty schedule")
    with admission_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if rows:
        raise RuntimeError(f"{admission_path}: empty transport admitted objects")
    return {
        "validated": True,
        "scheduled_records": 0,
        "admitted_records": 0,
        "admission_policy": "lowest importance rank among currently eligible records",
    }


def _run_case(
    *,
    client,
    server,
    background_sink,
    client_background_source,
    client_background_sink,
    provider_ingress: str,
    provider_egress: str,
    downstream_egress: str,
    experiment_root: Path,
    split_root: Path,
    manifest: dict[str, object],
    fraction: float,
    repetition: int,
    args,
) -> dict[str, object]:
    tag = f"{fraction:.4f}".rstrip("0").rstrip(".").replace(".", "p")
    case = experiment_root / f"l4s-{tag}-rep-{repetition:02d}"
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
    # Captures start only after both QUIC connections are established. This
    # excludes handshake/startup packets from the reordering evidence.
    captures = {}
    processes = []
    streams = []
    publisher = None
    subscribers = {}
    subscriber_logs = {}
    ready_path = case / "publisher-ready"
    go_path = case / "workload-go.txt"
    workload_start_epoch_us = None
    background_started_epoch_us = None
    background_alive_after_warmup = False
    background_alive_through_workload = False
    bg_client = None
    client_bg_client = None

    try:
        if _background_enabled(args):
            server.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
            background_sink.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
            bg_server_log = (case / "background-server.json").open("w")
            bg_client_log = (case / "background-client.json").open("w")
            streams += [bg_server_log, bg_client_log]
            bg_server = background_sink.popen(
                ["iperf3", "-s", "-1", "-p", "5201", "--json"],
                stdout=bg_server_log,
                stderr=subprocess.STDOUT,
            )
            processes.append(bg_server)
            time.sleep(0.2)
            duration = (
                args.dc_background_warmup_s
                + args.prague_warmup_ms / 1000.0
                + args.deadline_ms / 1000.0
                + args.subscriber_guard_ms / 1000.0
                + PRAGUE_WARMUP_DRAIN_GUARD_MS / 1000.0
                + 2
            )
            bg_client = server.popen(
                _iperf_background_command(
                    background_sink.IP(), duration, args.dc_background_mbps,
                    args.dc_background_cc,
                ),
                stdout=bg_client_log,
                stderr=subprocess.STDOUT,
            )
            processes.append(bg_client)
            if client_background_source is not None and client_background_sink is not None:
                client_background_source.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
                client_background_sink.cmd("sysctl -qw net.ipv4.tcp_ecn=0")
                client_bg_server_log = (
                    case / "client-switch-background-server.json"
                ).open("w")
                client_bg_client_log = (
                    case / "client-switch-background-client.json"
                ).open("w")
                streams += [client_bg_server_log, client_bg_client_log]
                client_bg_server = client_background_sink.popen(
                    ["iperf3", "-s", "-1", "-p", "5202", "--json"],
                    stdout=client_bg_server_log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(client_bg_server)
                time.sleep(0.2)
                client_bg_client = client_background_source.popen(
                    _iperf_background_command(
                        client_background_sink.IP(), duration,
                        args.dc_background_mbps, args.dc_background_cc, 5202,
                    ),
                    stdout=client_bg_client_log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(client_bg_client)
            background_started_epoch_us = time.time_ns() // 1000
            time.sleep(args.dc_background_warmup_s)

        # One publisher owns both persistent media connections in every case.
        for name in PATHS:
            path_root = case / name
            path_root.mkdir()
            sub_log = (path_root / "subscriber.log").open("w")
            streams.append(sub_log)
            subscriber_logs[name] = sub_log

        publisher_log = (case / "publisher.log").open("w")
        streams.append(publisher_log)
        publisher_command = [
            str(BINARY), "publisher-scheduled-dual-gated", server.IP(),
            str(args.deadline_ms), str(args.transport_queue_slack_bytes),
            str(args.prague_warmup_ms), str(ready_path), str(go_path),
            str(case / "combined-admission-order.csv"),
            str(case / "scheduler-result.json"),
        ]
        for name in ("high-prague", "low-reno"):
            path_root = case / name
            path_input = manifest["path_inputs"][name]
            publisher_command.extend([
                str(path_input["bundle"]),
                str(path_root / "transport-metrics.csv"),
                str(path_root / "publisher-result.json"),
                str(path_input["schedule"]["path"]),
                str(path_root / "admission-order.csv"),
            ])
        publisher = server.popen(
            publisher_command,
            cwd=str(ROOT / "deps" / "imquic" / "src"),
            stdout=publisher_log,
            stderr=subprocess.STDOUT,
        )
        processes.append(publisher)

        time.sleep(0.3)
        launch_order = list(PATHS)
        if repetition % 2 == 0:
            launch_order.reverse()
        subscriber_deadline_ms = (
            args.prague_warmup_ms
            + PRAGUE_WARMUP_DRAIN_GUARD_MS
            + args.deadline_ms
            + args.subscriber_guard_ms
        )
        for name in launch_order:
            config = PATHS[name]
            path_root = case / name
            subscriber_command = [
                str(BINARY), "subscriber", server.IP(), str(config["port"]),
                config["mode"], str(path_root / "received.bundle"),
                str(subscriber_deadline_ms), str(path_root / "arrival-timeline.csv"),
                str(path_root / "subscriber-result.json"),
            ]
            # A valid Prague-first schedule may starve the Classic path until
            # the deadline while Prague waits for transport admission room.
            # The subscriber still reports connection failures, but an empty
            # completed stream is valid evidence for that scheduler outcome.
            subscriber_command.append("allow-empty")
            subscribers[name] = client.popen(
                subscriber_command,
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=subscriber_logs[name],
                stderr=subprocess.STDOUT,
            )
            processes.append(subscribers[name])

        _wait_publisher_ready(
            ready_path,
            publisher,
            args.endpoint_ready_timeout_s + args.prague_warmup_ms / 1000.0 + 5.0,
        )
        background_clients = [
            process for process in (bg_client, client_bg_client)
            if process is not None
        ]
        if background_clients:
            background_alive_after_warmup = all(
                process.poll() is None for process in background_clients
            )
            if not background_alive_after_warmup:
                raise RuntimeError(f"{case.name}: background ended during Prague warm-up")

        if args.capture_mode != "none":
            seen_interfaces = set()
            for label, interface in {
                "provider_ingress": provider_ingress,
                "provider_egress": provider_egress,
                "downstream_egress": downstream_egress,
            }.items():
                if interface in seen_interfaces:
                    continue
                seen_interfaces.add(interface)
                if args.capture_mode == "pcap":
                    captures[label] = _pcap_capture(
                        interface, case / f"{label}.pcap", server_ip=server.IP()
                    )
                else:
                    captures[label] = _packet_log_capture(
                        interface, case / f"{label}.packet-order.csv",
                        server_ip=server.IP(),
                    )
            time.sleep(args.capture_settle_ms / 1000.0)

        workload_start_epoch_us = (
            time.time_ns() // 1000 + int(args.workload_start_lead_ms * 1000)
        )
        # Publish the gate atomically.  Publishers poll this path concurrently,
        # so write_text() could expose a transient zero-length file and make a
        # publisher reject an otherwise valid gate.
        go_pending_path = go_path.with_suffix(f"{go_path.suffix}.pending")
        go_pending_path.write_text(f"{workload_start_epoch_us}\n", encoding="utf-8")
        go_pending_path.replace(go_path)

        timeout = (args.deadline_ms + args.subscriber_guard_ms) / 1000.0 + 30
        status = {
            f"{name}-subscriber": process.wait(timeout=timeout)
            for name, process in subscribers.items()
        }
        status["dual-publisher"] = publisher.wait(timeout=timeout)
        if any(status.values()):
            raise RuntimeError(f"{case.name}: endpoint failure: {status}")
        if background_clients:
            background_alive_through_workload = all(
                process.poll() is None for process in background_clients
            )
            if not background_alive_through_workload:
                raise RuntimeError(
                    f"{case.name}: background ended before workload completion"
                )
    finally:
        for process in reversed(processes):
            stop(process)
        for label, process in captures.items():
            _stop_capture(process, label)
        # In an isolated topology the active switch egress is both the
        # provider and downstream observation point.  Preserve the canonical
        # downstream filename expected by the analyzer without running a
        # second competing capture on the same interface.
        if downstream_egress == provider_egress:
            for suffix in ("packet-order.csv", "packet-order.stats.json"):
                source = case / f"provider_egress.{suffix}"
                target = case / f"downstream_egress.{suffix}"
                if source.is_file() and not target.exists():
                    shutil.copyfile(source, target)
        for stream in streams:
            stream.close()
        save_tc_state("after")

    if workload_start_epoch_us is None:
        raise RuntimeError(f"{case.name}: workload gate was never released")

    background_result = None
    if bg_client is not None:
        background_result = _read_background_result(case / "background-client.json")
    client_background_result = None
    if client_bg_client is not None:
        client_background_result = _read_background_result(
            case / "client-switch-background-client.json"
        )

    subscriber_deadline_ms = (
        args.prague_warmup_ms
        + PRAGUE_WARMUP_DRAIN_GUARD_MS
        + args.deadline_ms
        + args.subscriber_guard_ms
    )
    path_results = {
        name: validate_path(case / name, subscriber_deadline_ms)
        for name in PATHS
    }
    warmup_validation = {}
    for name in PATHS:
        publisher = path_results[name]["publisher"]
        subscriber = path_results[name]["subscriber"]
        queued_objects = int(publisher.get("warmup_queued_objects", 0))
        queued_bytes = int(publisher.get("warmup_queued_bytes", 0))
        received_objects = int(subscriber.get("warmup_received_objects", 0))
        received_bytes = int(subscriber.get("warmup_received_bytes", 0))
        if queued_objects != received_objects or queued_bytes != received_bytes:
            raise RuntimeError(
                f"{case.name}/{name}: incomplete warm-up discard delivery"
            )
        if name == "low-reno" and (queued_objects or queued_bytes):
            raise RuntimeError(f"{case.name}: Classic media connection was warmed")
        warmup_validation[name] = {
            "queued_objects": queued_objects,
            "queued_bytes": queued_bytes,
            "received_and_discarded_objects": received_objects,
            "received_and_discarded_bytes": received_bytes,
            "validated": True,
        }
    admission_results = {}
    for name, config in PATHS.items():
        schedule_path = Path(str(manifest["path_inputs"][name]["schedule"]["path"]))
        admission_path = case / name / "admission-order.csv"
        assigned = int(manifest[config["manifest_key"]]["objects"])
        if assigned:
            admission_results[name] = validate_admission_order(schedule_path, admission_path)
        else:
            admission_results[name] = _empty_admission_validation(
                schedule_path, admission_path
            )
    cross_path_admission = validate_cross_path_admission_order(
        Path(str(manifest["path_inputs"]["high-prague"]["schedule"]["path"])),
        Path(str(manifest["path_inputs"]["low-reno"]["schedule"]["path"])),
        case / "combined-admission-order.csv",
    )
    scheduler_result = json.loads(
        (case / "scheduler-result.json").read_text(encoding="utf-8")
    )
    if not scheduler_result.get("validated"):
        raise RuntimeError(f"{case.name}: scheduler result failed validation")
    if args.prague_warmup_ms > 0 and _background_enabled(args):
        if (
            background_started_epoch_us is None
            or background_started_epoch_us
            > int(scheduler_result["warmup_started_epoch_us"])
            or not background_alive_after_warmup
        ):
            raise RuntimeError(
                f"{case.name}: background did not span the Prague warm-up"
            )

    publisher_actual_starts = {
        name: int(path_results[name]["publisher"]["publisher_started_epoch_us"])
        for name in PATHS
    }
    requested_starts = {
        name: int(path_results[name]["publisher"]["workload_start_epoch_us"])
        for name in PATHS
    }
    if any(value != workload_start_epoch_us for value in requested_starts.values()):
        raise RuntimeError(f"{case.name}: publishers did not consume the same workload gate")
    publisher_start_skew_us = max(publisher_actual_starts.values()) - min(
        publisher_actual_starts.values()
    )
    publisher_start_lateness_us = max(
        abs(value - workload_start_epoch_us) for value in publisher_actual_starts.values()
    )
    if publisher_start_skew_us > int(args.max_start_skew_ms * 1000):
        raise RuntimeError(
            f"{case.name}: workload start skew {publisher_start_skew_us / 1000:.3f} ms "
            f"exceeds {args.max_start_skew_ms:.3f} ms"
        )
    if publisher_start_lateness_us > int(args.max_start_lateness_ms * 1000):
        raise RuntimeError(
            f"{case.name}: workload gate lateness "
            f"{publisher_start_lateness_us / 1000:.3f} ms exceeds "
            f"{args.max_start_lateness_ms:.3f} ms"
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
        "requested_l4s_byte_fraction": fraction,
        "actual_l4s_byte_fraction": balance["actual_l4s_byte_fraction"],
        "workload_start_epoch_us": workload_start_epoch_us,
        "publisher_start_epoch_us": {name: workload_start_epoch_us for name in PATHS},
        "publisher_actual_start_epoch_us": publisher_actual_starts,
        "publisher_start_skew_us": publisher_start_skew_us,
        "publisher_start_lateness_us": publisher_start_lateness_us,
        "transport_queue_slack_bytes": args.transport_queue_slack_bytes,
        "prague_warmup_ms": args.prague_warmup_ms,
        "warmup_validation": warmup_validation,
        "warmup_phase": {
            "started_epoch_us": scheduler_result["warmup_started_epoch_us"],
            "finished_epoch_us": scheduler_result["warmup_finished_epoch_us"],
            "background_started_epoch_us": background_started_epoch_us,
            "background_active_through_warmup": background_alive_after_warmup,
            "background_active_through_workload": background_alive_through_workload,
            "background_overlap_validated": (
                args.prague_warmup_ms > 0
                and _background_enabled(args)
                and background_alive_after_warmup
                and background_alive_through_workload
            ),
            "classic_warmup_bytes": warmup_validation["low-reno"]["queued_bytes"],
        },
        "background": background_result,
        "client_switch_background": client_background_result,
        "background_flows": {
            "server_to_background_sink": background_result,
            "client_switch_dedicated": client_background_result,
        },
        "scheduler": scheduler_result,
        "cross_path_admission_validation": cross_path_admission,
        "priority_definition": manifest["priority_definition"],
        "connections": 2,
        "paths": {
            name: {
                "congestion": PATHS[name]["mode"],
                "port": PATHS[name]["port"],
                "assigned_objects": int(manifest[PATHS[name]["manifest_key"]]["objects"]),
                "assigned_payload_bytes": int(
                    manifest[PATHS[name]["manifest_key"]]["payload_bytes"]
                ),
                "received_objects": path_results[name]["bundle"]["objects"],
                "received_payload_bytes": path_results[name]["bundle"]["payload_bytes"],
                "admission_validation": admission_results[name],
            }
            for name in PATHS
        },
        "combined_timeline": combined,
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
    parser.add_argument(
        "--3dgs-dir", dest="three_dgs_dir", type=Path, default=DEFAULT_3DGS
    )
    parser.add_argument("--allow-unpinned-3dgs", action="store_true")
    parser.add_argument(
        "--l4s-fractions",
        "--enhancement-l4s-fractions",
        dest="l4s_fractions",
        type=_fractions,
        default=_fractions("0,0.25,0.5,0.75,1"),
        help=(
            "fraction of total scene payload assigned to Prague/L4S by the "
            "(layer, mean-opacity) ranking"
        ),
    )
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--initial-release-ms", type=float, default=5.0)
    parser.add_argument("--demand-time-scale", type=float, default=1.0)
    parser.add_argument("--initial-visibility-spread-ms", type=float, default=10000.0)
    parser.add_argument("--fallback-track-spacing-ms", type=float, default=100.0)
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument(
        "--prague-warmup-ms",
        type=int,
        default=10000,
        help=(
            "send and discard saturated dummy objects on the same Prague "
            "connection before releasing the scene gate"
        ),
    )
    parser.add_argument("--subscriber-guard-ms", type=int, default=3000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--l4s-rate", default="300mbit")
    parser.add_argument("--l4s-burst", default="512k")
    parser.add_argument("--classic-rate", default="50mbit")
    parser.add_argument("--classic-burst", default="128k")
    parser.add_argument("--classic-buffer-packets", type=int, default=128)
    parser.add_argument(
        "--downstream-mode", choices=("classic", "dualpi2"), default="classic"
    )
    parser.add_argument(
        "--switch-topology", choices=("two-switch", "server-switch", "client-switch"),
        default="two-switch",
        help="two-switch baseline, isolated server-side L4S switch, or isolated client-side switch",
    )
    parser.add_argument("--isolated-switch-rate", default="200mbit")
    parser.add_argument("--base-rtt-ms", type=float, default=20.0)
    parser.add_argument(
        "--dualpi2-target",
        help="Classic target (default: 20ms for client-switch, otherwise 15ms)",
    )
    parser.add_argument(
        "--dualpi2-tupdate",
        help="PI update interval (default: 32ms for client-switch, otherwise 16ms)",
    )
    parser.add_argument(
        "--dualpi2-step",
        help="L4S step threshold (default: 15ms for client-switch, otherwise 5ms)",
    )
    parser.add_argument(
        "--dc-background-mbps", type=_background_rate, default=280.0,
        help="TCP background rate in Mbit/s, unlimited to omit iperf3 -b, or 0 to disable",
    )
    parser.add_argument(
        "--dc-background-cc", choices=("reno", "cubic"), default="reno"
    )
    parser.add_argument("--dc-background-warmup-s", type=float, default=2.0)
    parser.add_argument(
        "--transport-queue-slack-bytes",
        type=int,
        default=4096,
        help=(
            "admit the next application object only when QUIC "
            "queued_stream_bytes < min(object_size, this value) and "
            "bytes_in_flight < cwnd"
        ),
    )
    parser.add_argument("--endpoint-ready-timeout-s", type=float, default=10.0)
    parser.add_argument("--workload-start-lead-ms", type=float, default=200.0)
    parser.add_argument(
        "--capture-settle-ms",
        type=float,
        default=50.0,
        help="time to let packet capture attach after both QUIC connections are ready",
    )
    parser.add_argument("--max-start-skew-ms", type=float, default=2.0)
    parser.add_argument("--max-start-lateness-ms", type=float, default=5.0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--capture-mode", choices=("packet-log", "pcap", "none"),
        default="packet-log",
    )
    parser.add_argument("--no-pcap", action="store_true")
    args = parser.parse_args()
    client_dualpi2 = (
        args.switch_topology == "client-switch"
        and args.downstream_mode == "dualpi2"
    )
    if args.dualpi2_target is None:
        args.dualpi2_target = "20ms" if client_dualpi2 else "15ms"
    if args.dualpi2_tupdate is None:
        args.dualpi2_tupdate = "32ms" if client_dualpi2 else "16ms"
    if args.dualpi2_step is None:
        args.dualpi2_step = "15ms" if client_dualpi2 else "5ms"

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
    if _background_enabled(args) and shutil.which("iperf3") is None:
        raise SystemExit("iperf3 is required for datacenter aggregation background")
    if args.frozen_demand is None and args.trace is None:
        parser.error("--trace is required unless --frozen-demand is used")

    required_paths = [args.source_bundle]
    if args.frozen_demand is not None:
        required_paths.append(args.frozen_demand)
    else:
        required_paths.extend((args.trace, args.three_dgs_dir))
    for path in required_paths:
        if not path.exists():
            raise SystemExit(f"missing required path: {path}")

    if args.frame_stride <= 0 or args.demand_time_scale <= 0 or args.initial_visibility_spread_ms < 0:
        parser.error("frame stride and demand time scale must be positive; visibility spread must be non-negative")
    if args.repetitions <= 0 or args.deadline_ms <= 0:
        parser.error("repetitions and deadline must be positive")
    if args.prague_warmup_ms < 0:
        parser.error("--prague-warmup-ms must be non-negative")
    if args.subscriber_guard_ms <= 0:
        parser.error("--subscriber-guard-ms must be positive")
    if args.transport_queue_slack_bytes <= 0:
        parser.error("--transport-queue-slack-bytes must be positive")
    if args.base_rtt_ms < 0:
        parser.error("--base-rtt-ms must be non-negative")
    if (
        args.endpoint_ready_timeout_s <= 0
        or args.workload_start_lead_ms < 0
        or args.capture_settle_ms < 0
    ):
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
    client_background_source = None
    client_background_sink = None
    provider = net.addSwitch("s1", failMode="standalone")
    downstream = net.addSwitch("s2", failMode="standalone")
    if args.switch_topology == "server-switch":
        net.addLink(server, provider)
        net.addLink(provider, client)
        net.addLink(provider, background_sink)
    elif args.switch_topology == "client-switch":
        net.addLink(server, downstream)
        net.addLink(downstream, client)
        net.addLink(downstream, background_sink)
    else:
        net.addLink(server, provider)
        net.addLink(provider, downstream)
        net.addLink(downstream, client)
        net.addLink(downstream, background_sink)
        # Keep generated veth names below Linux IFNAMSIZ (15 visible chars).
        client_background_source = net.addHost("bg_src", ip="10.0.0.4/24")
        client_background_sink = net.addHost("bg_sink2", ip="10.0.0.5/24")
        net.addLink(client_background_source, provider)
        net.addLink(client_background_sink, downstream)

    results = []
    try:
        net.start()
        subprocess.run(["modprobe", "sch_dualpi2"], check=True)
        server_egress = _interface(
            server,
            provider if args.switch_topology != "client-switch" else downstream,
        )
        if args.switch_topology == "server-switch":
            provider_ingress = _interface(provider, server)
            provider_egress = _interface(provider, client)
            downstream_ingress = provider_ingress
            downstream_egress = provider_egress
            client_egress = _interface(client, provider)
            background_egress = _interface(background_sink, provider)
        elif args.switch_topology == "client-switch":
            provider_ingress = _interface(downstream, server)
            provider_egress = _interface(downstream, client)
            downstream_ingress = provider_ingress
            downstream_egress = provider_egress
            client_egress = _interface(client, downstream)
            background_egress = _interface(background_sink, downstream)
        else:
            provider_ingress = _interface(provider, server)
            provider_egress = _interface(provider, downstream)
            downstream_ingress = _interface(downstream, provider)
            downstream_egress = _interface(downstream, client)
            client_egress = _interface(client, downstream)
            background_egress = _interface(background_sink, downstream)

        # base_rtt_ms is split equally between media data and ACK directions.
        one_way_ms = args.base_rtt_ms / 2.0
        _configure_fixed_delay(server, server_egress, one_way_ms)
        _configure_fixed_delay(client, client_egress, one_way_ms)
        _configure_fixed_delay(background_sink, background_egress, one_way_ms)
        if client_background_source is not None and client_background_sink is not None:
            _configure_fixed_delay(
                client_background_source,
                _interface(client_background_source, provider),
                one_way_ms,
            )
            _configure_fixed_delay(
                client_background_sink,
                _interface(client_background_sink, downstream),
                one_way_ms,
            )

        offload_evidence = {}
        if args.capture_mode != "none":
            offload_evidence = _disable_offloads([
                server_egress, provider_ingress, provider_egress,
                downstream_ingress, downstream_egress, client_egress,
            ])

        write_experiment_record(
            args.output,
            scenario="3dgs-two-connection-l4s-spectrum-reordering",
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
                "connections": 2,
                "transport_assignment": (
                    "ranked payload-byte prefix -> Prague; remainder -> Reno"
                ),
                "application_priority": (
                    "(progressive layer/subgroup ascending, mean opacity descending, "
                    "stable source order)"
                ),
                "application_admission_gate": (
                    "Prague-first high-biased two-queue select; Classic only while "
                    "Prague has no eligible object; selected transport requires "
                    "queued_stream_bytes < min(object_size, transport_queue_slack_bytes) "
                    "and bytes_in_flight < cwnd"
                ),
                "transport_queue_slack_bytes": args.transport_queue_slack_bytes,
                "prague_warmup_ms": args.prague_warmup_ms,
                "warmup_assignment": "Prague only; Classic media connection idle",
                "background_during_warmup": _background_enabled(args),
                "demand_time_scale": args.demand_time_scale,
                "initial_visibility_spread_ms": args.initial_visibility_spread_ms,
                "eligibility_granularity": "object-aabb",
                "never_visible_policy": "excluded-from-visible-workload",
                "initial_release_ms": args.initial_release_ms,
                "base_rtt_ms": args.base_rtt_ms,
                "dc_background_mbps": args.dc_background_mbps,
                "dc_background_rate_mode": (
                    "unlimited" if args.dc_background_mbps is None else "limited"
                ),
                "dc_background_cc": args.dc_background_cc,
                "client_switch_background": client_background_source is not None,
                "client_switch_background_rate_mode": (
                    "unlimited" if args.dc_background_mbps is None else "limited"
                ),
                "client_switch_background_cc": args.dc_background_cc,
                "provider_dualpi2_rate": args.l4s_rate,
                "downstream_rate": args.classic_rate,
                "downstream_mode": args.downstream_mode,
                "switch_topology": args.switch_topology,
                "isolated_switch_rate": args.isolated_switch_rate,
                "classic_buffer_packets": args.classic_buffer_packets,
                "dualpi2_target": args.dualpi2_target,
                "dualpi2_tupdate": args.dualpi2_tupdate,
                "dualpi2_step": args.dualpi2_step,
                "deadline_ms": args.deadline_ms,
                "repetitions": args.repetitions,
                "packet_order_capture": args.capture_mode,
                "capture_start": (
                    "after both QUIC connections are ready, before workload gate"
                ),
            },
            topology={
                "forward": (
                    "server -> s1(DualPI2) -> client" if args.switch_topology == "server-switch" else
                    ("server -> s2(%s) -> client" % ("DualPI2" if args.downstream_mode == "dualpi2" else "FIFO"))
                    if args.switch_topology == "client-switch" else
                    "server -> s1(DualPI2) -> " + ("s2(DualPI2) -> client" if args.downstream_mode == "dualpi2" else "s2(FIFO) -> client")
                ),
                "base_rtt": (
                    "base_rtt_ms/2 netem on server egress for data; "
                    "base_rtt_ms/2 netem on client egress for ACKs"
                ),
                "aggregation_background": (
                    "server -> s1 -> s2 -> background_sink; shares provider egress "
                    "but not client-facing qdisc"
                ),
                "client_switch_background": (
                    "bg_client_src -> s1 -> s2 -> bg_client_sink; dedicated "
                    "second-switch iperf flow, same configured rate mode"
                    if client_background_source is not None
                    else "disabled outside two-switch topology"
                ),
                "interfaces": {
                    "server_egress": server_egress,
                    "provider_ingress": provider_ingress,
                    "provider_egress": provider_egress,
                    "downstream_ingress": downstream_ingress,
                    "downstream_egress": downstream_egress,
                    "client_egress": client_egress,
                },
            },
            observed_network={"offload_features": offload_evidence},
        )

        for fraction in args.l4s_fractions:
            split_root, manifest = prepared[fraction]
            for repetition in range(1, args.repetitions + 1):
                results.append(_run_case(
                    client=client,
                    server=server,
                    background_sink=background_sink,
                    client_background_source=client_background_source,
                    client_background_sink=client_background_sink,
                    provider_ingress=provider_ingress,
                    provider_egress=provider_egress,
                    downstream_egress=downstream_egress,
                    experiment_root=args.output,
                    split_root=split_root,
                    manifest=manifest,
                    fraction=fraction,
                    repetition=repetition,
                    args=args,
                ))
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)

    (args.output / "summary.json").write_text(
        json.dumps({
            "scenario": "3dgs-two-connection-l4s-spectrum-reordering",
            "frozen_demand_order": str(
                args.output / "inputs" / "frozen-demand-order.json"
            ),
            "cases": results,
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
