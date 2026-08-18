#!/usr/bin/env python3
"""Run high-priority 3DGS objects over Prague and low-priority objects over Reno."""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import time
from itertools import zip_longest
from pathlib import Path

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSBridge

from experiment_metadata import capture_mininet_state, detailed_tc_state, write_experiment_record
from object_timeline import summarize_timeline
from split_3dgs_priority import object_identity, split_priority_bundle
from three_dgs_bundle import bundle_summary, read_bundle, sha256_file

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "imquic-3dgs-moq"
PATHS = {
    "low-reno": {"priority": "low", "mode": "reno", "port": 4443},
    "high-prague": {"priority": "high", "mode": "prague", "port": 4444},
}


def stop(process) -> None:
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def configure_dualpi2(switch, args) -> None:
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
             "classid", "1:1", "htb", "rate", args.bottleneck,
             "burst", args.htb_burst, "cburst", args.htb_burst], check=True,
        )
        subprocess.run(
            ["tc", "qdisc", "add", "dev", device, "parent", "1:1",
             "handle", "10:", "dualpi2", "target", args.dualpi2_target,
             "tupdate", args.dualpi2_tupdate, "step_thresh", args.dualpi2_step_thresh],
            check=True,
        )


def save_qdisc_stats(switch, case: Path, label: str) -> None:
    with (case / "dualpi2-stats.txt").open("a", encoding="utf-8") as stream:
        stream.write(f"snapshot={label}\n")
        for interface in (f"{switch.name}-eth1", f"{switch.name}-eth2"):
            stream.write(f"device={interface}\n{detailed_tc_state(interface)}")


def validate_path(path_dir: Path, deadline_ms: int) -> dict[str, object]:
    publisher = json.loads((path_dir / "publisher-result.json").read_text(encoding="utf-8"))
    subscriber = json.loads((path_dir / "subscriber-result.json").read_text(encoding="utf-8"))
    if not publisher.get("validated") or not subscriber.get("validated"):
        raise RuntimeError(f"{path_dir}: endpoint validation failed")
    bundle = bundle_summary(path_dir / "received.bundle")
    timeline = summarize_timeline(path_dir / "arrival-timeline.csv")
    if bundle["objects"] != subscriber["received_objects"] or timeline["objects"] != bundle["objects"]:
        raise RuntimeError(f"{path_dir}: object-count mismatch")
    if bundle["payload_bytes"] != timeline["payload_bytes"]:
        raise RuntimeError(f"{path_dir}: byte-count mismatch")
    if timeline["gaussians"] != subscriber["received_gaussians"]:
        raise RuntimeError(f"{path_dir}: Gaussian-count mismatch")
    last = timeline["last_arrival_us"]
    if last is not None and int(last) > deadline_ms * 1000:
        raise RuntimeError(f"{path_dir}: post-deadline object in timeline")
    return {
        "publisher": publisher,
        "subscriber": subscriber,
        "bundle": bundle,
        "timeline": timeline,
    }


def write_combined_timeline(
    case: Path,
    path_results: dict[str, dict[str, object]],
    split_manifest: dict[str, object],
) -> dict[str, object]:
    rows = []
    origins = []
    manifest_by_identity = {}
    for object_row in split_manifest["objects"]:
        identity = (
            object_row["track_id"], object_row["group_id"],
            int(object_row["subgroup_id"]), int(object_row["object_id"]),
        )
        if identity in manifest_by_identity:
            raise RuntimeError(f"duplicate source object identity in priority manifest: {identity}")
        manifest_by_identity[identity] = object_row

    for name, config in PATHS.items():
        result = path_results[name]
        subscriber = result["subscriber"]
        start_epoch_us = int(subscriber["started_epoch_us"])
        origins.append(start_epoch_us)
        timeline_path = case / name / "arrival-timeline.csv"
        received_path = case / name / "received.bundle"
        missing = object()
        with timeline_path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            for row, payload in zip_longest(reader, read_bundle(received_path), fillvalue=missing):
                if row is missing or payload is missing:
                    raise RuntimeError(f"{case / name}: timeline/received-bundle length mismatch")
                path_time_us = int(row["arrival_time_us"])
                path_record_index = int(row["bundle_record_index"])
                identity_fields = object_identity(payload)
                identity = (
                    identity_fields["track_id"], identity_fields["group_id"],
                    int(identity_fields["subgroup_id"]), int(identity_fields["object_id"]),
                )
                source = manifest_by_identity.get(identity)
                if source is None:
                    raise RuntimeError(
                        f"{case / name}: received object missing from priority manifest: {identity}"
                    )
                expected_transport = "prague" if name == "high-prague" else "reno"
                if source["transport"] != expected_transport:
                    raise RuntimeError(
                        f"{case / name}: object {identity} arrived on {expected_transport} "
                        f"but manifest assigns {source['transport']}"
                    )
                rows.append({
                    "absolute_epoch_us": start_epoch_us + path_time_us,
                    "path": name,
                    "priority": config["priority"],
                    "congestion": config["mode"],
                    "path_arrival_time_us": path_time_us,
                    "bundle_record_index": path_record_index,
                    "source_record_index": int(source["source_record_index"]),
                    "importance_score": float(source["importance_score"]),
                    "importance_rank": int(source["importance_rank"]),
                    "payload_bytes": int(row["payload_bytes"]),
                    "num_gaussians": int(row["num_gaussians"]),
                    "track_id": identity[0],
                    "group_id": identity[1],
                    "subgroup_id": int(row["subgroup_id"]),
                    "object_id": int(row["object_id"]),
                })
    origin = min(origins)
    rows.sort(key=lambda row: (row["absolute_epoch_us"], row["path"], row["bundle_record_index"]))
    destination = case / "combined-arrival-timeline.csv"
    fields = (
        "experiment_time_us", "absolute_epoch_us", "path", "priority", "congestion",
        "path_arrival_time_us", "bundle_record_index", "source_record_index",
        "importance_score", "importance_rank", "payload_bytes", "num_gaussians", "track_id", "group_id",
        "subgroup_id", "object_id",
    )
    cumulative_splats = {"low-reno": 0, "high-prague": 0}
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({"experiment_time_us": row["absolute_epoch_us"] - origin, **row})
            cumulative_splats[row["path"]] += row["num_gaussians"]
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "origin_epoch_us": origin,
        "objects": len(rows),
        "received_gaussians": cumulative_splats,
    }


def run_case(
    client, server, switch, root: Path, inputs: Path, split_manifest: dict[str, object],
    repetition: int, args,
) -> dict[str, object]:
    case = root / f"rep-{repetition:02d}"
    case.mkdir(parents=True, exist_ok=False)
    configure_dualpi2(switch, args)
    save_qdisc_stats(switch, case, "before")

    publishers = {}
    subscribers = {}
    publisher_logs = {}
    subscriber_logs = {}
    streams = []
    try:
        for name, config in PATHS.items():
            path_dir = case / name
            path_dir.mkdir()
            publisher_log = (path_dir / "publisher.log").open("w", encoding="utf-8")
            subscriber_log = (path_dir / "subscriber.log").open("w", encoding="utf-8")
            publisher_logs[name] = publisher_log
            subscriber_logs[name] = subscriber_log
            streams.extend((publisher_log, subscriber_log))
            source_bundle = inputs / ("high-priority.bundle" if config["priority"] == "high" else "low-priority.bundle")
            publishers[name] = server.popen(
                [str(BINARY), "publisher", server.IP(), str(config["port"]), config["mode"],
                 str(source_bundle), str(args.deadline_ms),
                 str(path_dir / "transport-metrics.csv"), str(path_dir / "publisher-result.json")],
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=publisher_logs[name], stderr=subprocess.STDOUT,
            )

        time.sleep(0.4)
        for name, process in publishers.items():
            if process.poll() is not None:
                raise RuntimeError(f"{case.name}: {name} publisher exited before subscription")

        # Alternate launch order so the sequential popen calls cannot consistently
        # give either congestion controller a startup advantage.
        launch_order = ["high-prague", "low-reno"] if repetition % 2 else ["low-reno", "high-prague"]
        launch_epochs = {}
        for name in launch_order:
            config = PATHS[name]
            path_dir = case / name
            launch_epochs[name] = time.time_ns() // 1000
            subscribers[name] = client.popen(
                [str(BINARY), "subscriber", server.IP(), str(config["port"]), config["mode"],
                 str(path_dir / "received.bundle"), str(args.deadline_ms),
                 str(path_dir / "arrival-timeline.csv"), str(path_dir / "subscriber-result.json")],
                cwd=str(ROOT / "deps" / "imquic" / "src"),
                stdout=subscriber_logs[name], stderr=subprocess.STDOUT,
            )

        timeout = args.deadline_ms / 1000.0 + 20
        statuses = {}
        for name, process in subscribers.items():
            statuses[f"{name}_subscriber"] = process.wait(timeout=timeout)
        for name, process in publishers.items():
            statuses[f"{name}_publisher"] = process.wait(timeout=timeout)
        if any(statuses.values()):
            raise RuntimeError(f"{case.name}: endpoint failure {statuses}")
    finally:
        for process in [*subscribers.values(), *publishers.values()]:
            stop(process)
        save_qdisc_stats(switch, case, "after")
        for stream in streams:
            stream.close()

    path_results = {
        name: validate_path(case / name, args.deadline_ms)
        for name in PATHS
    }
    endpoint_starts = {
        name: int(path_results[name]["subscriber"]["started_epoch_us"])
        for name in PATHS
    }
    start_skew_us = max(endpoint_starts.values()) - min(endpoint_starts.values())
    if start_skew_us > int(args.max_start_skew_ms * 1000):
        raise RuntimeError(
            f"{case.name}: subscriber endpoint start skew {start_skew_us / 1000:.3f} ms "
            f"exceeds {args.max_start_skew_ms:.3f} ms"
        )
    combined = write_combined_timeline(case, path_results, split_manifest)
    result = {
        "case": case.name,
        "repetition": repetition,
        "subscriber_launch_order": launch_order,
        "subscriber_launch_epoch_us": launch_epochs,
        "subscriber_endpoint_start_epoch_us": endpoint_starts,
        "subscriber_start_skew_us": start_skew_us,
        "split_balance": split_manifest["balance"],
        "paths": {
            name: {
                "priority": PATHS[name]["priority"],
                "congestion": PATHS[name]["mode"],
                "port": PATHS[name]["port"],
                "assigned_objects": split_manifest[PATHS[name]["priority"]]["objects"],
                "assigned_payload_bytes": split_manifest[PATHS[name]["priority"]]["payload_bytes"],
                "assigned_gaussians": split_manifest[PATHS[name]["priority"]]["gaussians"],
                "assigned_subgroup_objects": split_manifest[PATHS[name]["priority"]]["subgroup_objects"],
                "received_objects": path_results[name]["bundle"]["objects"],
                "received_payload_bytes": path_results[name]["bundle"]["payload_bytes"],
                "received_gaussians": path_results[name]["timeline"]["gaussians"],
                "first_arrival_us": path_results[name]["timeline"]["first_arrival_us"],
                "last_arrival_us": path_results[name]["timeline"]["last_arrival_us"],
            }
            for name in PATHS
        },
        "combined_timeline": combined,
    }
    (case / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument(
        "--importance",
        choices=("native-tier", "opacity", "scale", "opacity-scale"),
        default="native-tier",
    )
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--bottleneck", default="300mbit")
    parser.add_argument("--htb-burst", default="512k")
    parser.add_argument("--dualpi2-target", default="15ms")
    parser.add_argument("--dualpi2-tupdate", default="16ms")
    parser.add_argument("--dualpi2-step-thresh", default="1ms")
    parser.add_argument(
        "--max-start-skew-ms", type=float, default=50.0,
        help="reject a repetition if subscriber endpoint start clocks differ by more than this",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("3DGS priority-split experiment must run as root")
    if not BINARY.is_file():
        raise SystemExit(f"missing fixture binary: {BINARY}; run make build-3dgs-fixture-only")
    if not args.source_bundle.is_file():
        raise SystemExit(f"missing source bundle: {args.source_bundle}")
    if args.deadline_ms <= 0 or args.repetitions <= 0:
        parser.error("deadline and repetitions must be positive")
    if args.max_start_skew_ms < 0:
        parser.error("--max-start-skew-ms must be non-negative")

    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "inputs"
    split = split_priority_bundle(args.source_bundle, inputs, importance=args.importance)

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
            scenario="3dgs-priority-split-prague-reno",
            configuration={
                "source_bundle": str(args.source_bundle.resolve()),
                "source_sha256": sha256_file(args.source_bundle),
                "importance": args.importance,
                "split_rule": split["split_rule"],
                "split_balance": split["balance"],
                "source_order_preserved_within_each_output": True,
                "high_priority_transport": "Prague QUIC, ECT(1), port 4444",
                "low_priority_transport": "Reno QUIC, Not-ECT, port 4443",
                "connections": "concurrent and independent",
                "deadline_ms": args.deadline_ms,
                "repetitions": args.repetitions,
                "max_start_skew_ms": args.max_start_skew_ms,
                "bottleneck": args.bottleneck,
                "htb_burst": args.htb_burst,
                "dualpi2_target": args.dualpi2_target,
                "dualpi2_tupdate": args.dualpi2_tupdate,
                "dualpi2_step_thresh": args.dualpi2_step_thresh,
                "rendering": "disabled in network experiment; reconstruct offline from two bundles + timelines",
            },
            topology={
                "nodes": {"client": "10.0.0.1/24", "server": "10.0.0.2/24", "switch": "s1 OVSBridge"},
                "links": ["client<->s1", "s1<->server"],
                "forward_direction": "server-to-client on two simultaneous QUIC connections",
            },
            observed_network=capture_mininet_state(client, server, switch),
        )
        for repetition in range(1, args.repetitions + 1):
            results.append(
                run_case(client, server, switch, args.output, inputs, split, repetition, args)
            )
    finally:
        net.stop()
        subprocess.run(["mn", "-c"], check=False)

    summary = {
        "scenario": "3dgs-priority-split-prague-reno",
        "priority_manifest": str(inputs / "priority-manifest.json"),
        "cases": results,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
