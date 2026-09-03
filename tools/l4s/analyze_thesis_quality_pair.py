#!/usr/bin/env python3
"""Evaluate fixed-deadline SSIM recovery for a rendered Classic/L4S pair."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np


def trapezoid_integral(values: np.ndarray, time_ms: np.ndarray) -> float:
    if len(values) < 2:
        return 0.0
    return float(np.sum((values[:-1] + values[1:]) * np.diff(time_ms) / 2.0))


def read_quality(path: Path, wrap_ms: float) -> tuple[np.ndarray, np.ndarray]:
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    return (
        np.array([float(row["trace_timestamp_ms"]) - wrap_ms for row in rows]),
        np.array([float(row["ssim"]) for row in rows]),
    )


def summarize(path: Path, wrap_ms: float) -> dict:
    time_ms, ssim = read_quality(path, wrap_ms)
    post = time_ms >= 0
    first_two = post & (time_ms <= 2000)
    duration = float(time_ms[first_two][-1] - time_ms[first_two][0])
    area = trapezoid_integral(ssim[first_two], time_ms[first_two])
    recovery = None
    for index in np.where(post)[0]:
        window = (time_ms >= time_ms[index]) & (time_ms <= time_ms[index] + 1000)
        if (time_ms[window][-1] - time_ms[index] >= 960
                and np.all(ssim[window] >= 0.90)):
            recovery = float(time_ms[index]); break
    return {
        "frames": int(len(ssim)), "wrap_time_ms": wrap_ms,
        "first_two_second_ssim_area_ms": area,
        "first_two_second_mean_ssim": area / duration,
        "mean_post_wrap_ssim": float(np.mean(ssim[post])),
        "sustained_0p90_recovery_ms": recovery,
        "quality_csv": str(path),
    }


def cumulative_quality_crossover_ms(
    time_ms: np.ndarray, classic_ssim: np.ndarray, l4s_ssim: np.ndarray
) -> float | None:
    """Return the lasting post-wrap crossover of cumulative L4S SSIM gain."""
    post = time_ms >= 0
    post_time = time_ms[post]
    advantage = l4s_ssim[post] - classic_ssim[post]
    if len(post_time) < 2:
        return None
    cumulative = np.zeros(len(post_time), dtype=float)
    for index in range(1, len(post_time)):
        cumulative[index] = trapezoid_integral(
            advantage[:index + 1], post_time[:index + 1]
        )
    for index, value in enumerate(cumulative):
        if value >= 0 and np.all(cumulative[index:] >= 0):
            return float(post_time[index])
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--wrap-ms", type=float, required=True)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()
    paths = {
        "classic": next(args.root.glob("classic/l4s-*-rep-*/render-lag-0ms/frame-quality.csv")),
        "l4s": next(args.root.glob("l4s/l4s-*-rep-*/render-lag-0ms/frame-quality.csv")),
    }
    result = {label: summarize(path, args.wrap_ms) for label, path in paths.items()}
    classic_time, classic_ssim = read_quality(paths["classic"], args.wrap_ms)
    l4s_time, l4s_ssim = read_quality(paths["l4s"], args.wrap_ms)
    if not np.array_equal(classic_time, l4s_time):
        raise ValueError("Classic and L4S quality traces use different frame times")
    result["cumulative_quality_crossover_ms"] = cumulative_quality_crossover_ms(
        classic_time, classic_ssim, l4s_ssim
    )
    classic, l4s = result["classic"], result["l4s"]
    area_higher = l4s["first_two_second_ssim_area_ms"] > classic["first_two_second_ssim_area_ms"]
    if classic["sustained_0p90_recovery_ms"] is None and l4s["sustained_0p90_recovery_ms"] is None:
        recovery_support = l4s["mean_post_wrap_ssim"] > classic["mean_post_wrap_ssim"]
        recovery_rule = "neither reached sustained 0.90; compare post-wrap mean"
    elif l4s["sustained_0p90_recovery_ms"] is None:
        recovery_support = False; recovery_rule = "Classic recovered but L4S did not"
    elif classic["sustained_0p90_recovery_ms"] is None:
        recovery_support = True; recovery_rule = "only L4S recovered"
    else:
        recovery_support = l4s["sustained_0p90_recovery_ms"] <= classic["sustained_0p90_recovery_ms"]
        recovery_rule = "compare sustained 0.90 recovery time"
    result["checks"] = {
        "l4s_first_two_second_area_higher": area_higher,
        "l4s_recovery_support": recovery_support,
    }
    result["recovery_rule"] = recovery_rule
    result["primary_quality_gate"] = "post-wrap recovery support"
    result["initial_two_second_area_role"] = (
        "diagnostic Prague ramp-up interval; retained in the figure but not a gate"
    )
    result["supports_l4s_claim"] = recovery_support
    (args.root / "quality-pair-support.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["checks"], indent=2))
    if args.gate and not result["supports_l4s_claim"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
