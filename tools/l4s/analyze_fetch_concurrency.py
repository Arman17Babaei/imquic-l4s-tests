#!/usr/bin/env python3
"""Summarize and plot the IMQUIC concurrent-FETCH experiment."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def goodput_bins(rows: list[dict[str, str]], width: float = 0.5) -> tuple[list[float], list[float]]:
    if not rows:
        return [], []
    maximum = max(float(row["time_us"]) / 1_000_000 for row in rows)
    values = [0] * (math.floor(maximum / width) + 1)
    for row in rows:
        index = math.floor((float(row["time_us"]) / 1_000_000) / width)
        values[index] += int(row["payload_bytes"])
    times = [(index + 0.5) * width for index in range(len(values))]
    rates = [value * 8 / width / 1_000_000 for value in values]
    return times, rates


def parse_tc_totals(text: str) -> dict[str, int]:
    """Read the first (root HTB) Sent counter under each named direction."""
    totals: dict[str, int] = {}
    direction: str | None = None
    for line in text.splitlines():
        heading = re.fullmatch(r"\[([^]]+)]\s+\S+", line)
        if heading:
            direction = heading.group(1)
            continue
        sent = re.match(r"\s*Sent\s+(\d+)\s+bytes\b", line)
        if direction is not None and sent and direction not in totals:
            totals[direction] = int(sent.group(1))
    return totals


def load_cases(root: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for case_path in sorted(root.glob("n-*-rep-*")):
        metadata_path = case_path / "metadata.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        client = metadata.get("client_result", {})
        server = metadata.get("server_result", {})
        arrivals = read_csv(case_path / "arrival-timeline.csv")
        received_bytes = int(client.get("received_bytes", 0)) if isinstance(client, dict) else 0
        finish = float(client.get("finish_seconds", -1)) if isinstance(client, dict) else -1
        completed = bool(
            isinstance(client, dict)
            and client.get("validated")
            and int(client.get("fetches_completed", 0)) == int(metadata["n"])
            and received_bytes == int(metadata["total_bytes"])
        )
        wall = float(metadata.get("wall_duration_seconds", 0))
        tc_path = case_path / "tc-state-after.txt"
        tc_totals = parse_tc_totals(tc_path.read_text(encoding="utf-8")) \
            if tc_path.is_file() else {}
        cases.append({
            "path": case_path,
            "n": int(metadata["n"]),
            "repetition": int(metadata["repetition"]),
            "completed": completed,
            "status": metadata.get("status", "unknown"),
            "issue_seconds": float(client.get("issue_seconds", -1)) if isinstance(client, dict) else -1,
            "all_open_seconds": float(server.get("all_open_seconds", -1)) if isinstance(server, dict) else -1,
            "finish_seconds": finish if finish >= 0 else wall,
            "fetches_completed": int(client.get("fetches_completed", 0)) if isinstance(client, dict) else 0,
            "requests_received": int(server.get("requests_received", 0)) if isinstance(server, dict) else 0,
            "received_bytes": received_bytes,
            "mean_goodput_mbps": received_bytes * 8 /
            (finish if finish > 0 else wall) / 1_000_000
            if received_bytes and (finish > 0 or wall > 0) else 0,
            "server_to_client_link_bytes": tc_totals.get("server_to_client", 0),
            "client_to_server_link_bytes": tc_totals.get("client_to_server", 0),
            "arrivals": arrivals,
            "metrics": read_csv(case_path / "transport-metrics.csv"),
        })
    return cases


def write_summary(cases: list[dict[str, object]], output: Path) -> None:
    fields = [
        "n", "repetition", "completed", "status", "issue_seconds",
        "all_open_seconds", "finish_seconds", "fetches_completed",
        "requests_received", "received_bytes", "mean_goodput_mbps",
        "server_to_client_link_bytes", "client_to_server_link_bytes",
    ]
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            writer.writerow({field: case[field] for field in fields})
    serializable = [{key: value for key, value in case.items() if key not in {"path", "arrivals", "metrics"}} for case in cases]
    (output / "summary.json").write_text(json.dumps(serializable, indent=2) + "\n", encoding="utf-8")


def plots(cases: list[dict[str, object]], output: Path, timeout: float) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = plt.cm.viridis([index / max(1, len(cases) - 1) for index in range(len(cases))])
    figure, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    for color, case in zip(colors, cases):
        label = f"N={case['n']}"
        times, rates = goodput_bins(case["arrivals"])
        if times:
            axes[0].step(times, rates, where="mid", color=color, label=label, linewidth=1.2)
        else:
            axes[0].plot([], [], color=color, label=f"{label} (no payload)")
        metrics = case["metrics"]
        if metrics:
            metric_times = [float(row["time_us"]) / 1_000_000 for row in metrics]
            axes[1].plot(metric_times, [float(row["cwnd_bytes"]) / 1_000_000 for row in metrics], color=color, linewidth=1.0)
            axes[2].plot(metric_times, [float(row["rtt_us"]) / 1000 for row in metrics], color=color, linewidth=1.0)
    axes[0].axhline(20, color="black", linestyle="--", linewidth=0.8, label="20 Mbit/s link")
    axes[0].set_ylabel("Payload goodput (Mbit/s)")
    axes[1].set_ylabel("cwnd (MB)")
    axes[2].set_ylabel("Smoothed RTT (ms)")
    axes[2].set_xlabel("Time since process start (s)")
    axes[0].legend(ncol=4, fontsize=8)
    for axis in axes:
        axis.grid(True, alpha=0.25)
        axis.set_xlim(0, timeout)
    figure.tight_layout()
    for suffix in ("png", "svg"):
        figure.savefig(output / f"fetch-concurrency-timeseries.{suffix}", dpi=180)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(8, 4.5))
    for color, case in zip(colors, cases):
        marker = "o" if case["completed"] else "x"
        y = case["finish_seconds"]
        axis.scatter(case["n"], y, color=color, marker=marker, s=55)
    axis.axhline(timeout, color="red", linestyle="--", linewidth=1, label=f"{timeout:g} s cutoff")
    axis.axhline(40, color="black", linestyle=":", linewidth=1, label="40 s serialization floor")
    axis.set_xscale("log")
    axis.set_xlabel("Concurrent FETCH requests (N)")
    axis.set_ylabel("Experiment finish time (s)")
    axis.grid(True, which="both", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    for suffix in ("png", "svg"):
        figure.savefig(output / f"fetch-concurrency-finish-time.{suffix}", dpi=180)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(9, 4.8))
    positions = list(range(len(cases)))
    width = 0.38
    axis.bar(
        [position - width / 2 for position in positions],
        [case["server_to_client_link_bytes"] / 1_000_000 for case in cases],
        width, label="Server → client",
    )
    axis.bar(
        [position + width / 2 for position in positions],
        [case["client_to_server_link_bytes"] / 1_000_000 for case in cases],
        width, label="Client → server",
    )
    axis.set_xticks(positions, [str(case["n"]) for case in cases])
    axis.set_xlabel("Concurrent FETCH requests (N)")
    axis.set_ylabel("Link-layer transmitted data (MB)")
    axis.grid(True, axis="y", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    for suffix in ("png", "svg"):
        figure.savefig(output / f"fetch-concurrency-transmitted-data.{suffix}", dpi=180)
    plt.close(figure)


def self_test() -> None:
    rows = [
        {"time_us": "100000", "payload_bytes": "1000000"},
        {"time_us": "600000", "payload_bytes": "500000"},
    ]
    times, rates = goodput_bins(rows, 0.5)
    assert times == [0.25, 0.75]
    assert rates == [16.0, 8.0]
    totals = parse_tc_totals(
        "[server_to_client] s1-eth2\n[qdisc]\n"
        " Sent 1234 bytes 10 pkt\nqdisc dualpi2 10:\n"
        " Sent 1234 bytes 10 pkt\n"
        "[client_to_server] s1-eth1\n[qdisc]\n"
        " Sent 567 bytes 5 pkt\n"
    )
    assert totals == {"server_to_client": 1234, "client_to_server": 567}
    print("fetch-concurrency analyzer self-test: PASS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=80)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.input is None or args.output is None:
        parser.error("--input and --output are required unless --self-test is used")
    args.output.mkdir(parents=True, exist_ok=True)
    cases = load_cases(args.input)
    if not cases:
        raise SystemExit(f"no cases found under {args.input}")
    write_summary(cases, args.output)
    plots(cases, args.output, args.timeout)


if __name__ == "__main__":
    main()
