#!/usr/bin/env python3
"""Run the reviewed two-connection 3DGS L4S-spectrum experiment in QEMU."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fractions(value: str) -> str:
    try:
        fractions = [float(item) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "fractions must be comma-separated numbers in [0,1]"
        ) from error
    if not fractions or any(not 0.0 <= item <= 1.0 for item in fractions):
        raise argparse.ArgumentTypeError(
            "fractions must be comma-separated numbers in [0,1]"
        )
    return value


def _background_rate(value: str) -> float | None:
    if value.strip().lower() == "unlimited":
        return None
    try:
        rate = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "background rate must be a non-negative Mbit/s value or unlimited"
        ) from error
    if rate < 0:
        raise argparse.ArgumentTypeError("background rate must be non-negative")
    return rate


def _iperf_background_command(
    host: str, duration_s: float, rate_mbps: float | None,
    congestion_control: str, port: int = 5201,
) -> list[str]:
    command = ["iperf3", "-c", host, "-p", str(port), "-t", f"{duration_s:g}"]
    if rate_mbps is not None:
        command.extend(["-b", f"{rate_mbps}M"])
    command.extend(["-C", congestion_control, "--json"])
    return command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--frozen-demand", type=Path, required=True)
    parser.add_argument(
        "--l4s-fractions",
        type=_fractions,
        default="0,0.25,0.5,0.75,1",
        help=(
            "fraction of total scene payload assigned to the persistent Prague "
            "connection after ranking by (layer, mean opacity)"
        ),
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument("--qemu-memory", type=int, default=4096)
    parser.add_argument("--qemu-cpus", type=int, default=4)
    parser.add_argument("--guest-timeout", type=int, default=1800)
    parser.add_argument("--destination-prefix", default="qemu-3dgs-reordering")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--isolate-fractions",
        action="store_true",
        help="run each Prague-share cell in a fresh runner/Mininet process",
    )
    parser.add_argument("reordering_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    for path in (args.source_bundle, args.frozen_demand):
        if not path.is_file():
            raise SystemExit(f"missing input: {path}")
    if args.repetitions <= 0 or args.deadline_ms <= 0:
        parser.error("repetitions and deadline must be positive")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_3dgs_reordering*.py",
            "-v",
        ],
        cwd=ROOT,
        check=True,
    )

    experiment_args = [
        "--l4s-fractions",
        args.l4s_fractions,
        "--repetitions",
        str(args.repetitions),
        "--deadline-ms",
        str(args.deadline_ms),
        *[value for value in args.reordering_args if value != "--"],
    ]
    command = [
        sys.executable,
        str(ROOT / "tools/l4s/run_qemu_timeseries_test.py"),
        "--make-target",
        (
            "l4s-3dgs-reordering-matrix-check"
            if args.isolate_fractions
            else "l4s-3dgs-reordering-check"
        ),
        "--guest-result-name",
        "3dgs-reordering",
        "--guest-result-root",
        "results/l4s",
        "--destination-root",
        "results/l4s",
        "--destination-prefix",
        args.destination_prefix,
        "--guest-file",
        f"{args.source_bundle.resolve()}=inputs/scene.bundle",
        "--guest-file",
        f"{args.frozen_demand.resolve()}=inputs/frozen-demand-order.json",
        "--make-variable",
        "THREEDGS_BUNDLE=inputs/scene.bundle",
        "--make-variable",
        "THREEDGS_FROZEN_DEMAND=inputs/frozen-demand-order.json",
        "--make-variable",
        f"THREEDGS_REORDERING_ARGS={' '.join(experiment_args)}",
        "--memory",
        str(args.qemu_memory),
        "--cpus",
        str(args.qemu_cpus),
        "--guest-timeout",
        str(args.guest_timeout),
    ]
    if args.allow_dirty:
        command.append("--allow-dirty")
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
