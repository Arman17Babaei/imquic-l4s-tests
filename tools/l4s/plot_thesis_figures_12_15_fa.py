#!/usr/bin/env python3
"""Create Persian thesis Figures 12--15 from stored evidence and theory."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
import numpy as np

try:
    from thesis_topology import draw_pair_topology, draw_spectrum_topology
except ModuleNotFoundError:
    from tools.l4s.thesis_topology import draw_pair_topology, draw_spectrum_topology

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ModuleNotFoundError as error:
    raise SystemExit(
        "Persian shaping dependencies are required: pip install arabic-reshaper python-bidi"
    ) from error


RATE_BPS = 100_000_000.0
RTT_MS = 20.0
BDP_BYTES = RATE_BPS / 8.0 * RTT_MS / 1000.0
CONTROL_MAX = 4.0
IMPORTANT_FRACTION = 0.25
DEADLINE_MS = 30_000.0
INK = "#28323c"
GRID = "#d8dde3"
COLORS = {0.0: "#777777", 0.25: "#b44b4b", 0.5: "#d69b2d",
          0.75: "#1976a3", 1.0: "#1b7f5b"}
LABELS = {
    0.0: "فقط کلاسیک", 0.25: "دو جریان، ۲۵٪ L4S",
    0.5: "دو جریان، ۵۰٪ L4S", 0.75: "دو جریان، ۷۵٪ L4S",
    1.0: "تک‌جریان L4S (مبنا)",
}


def fa(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def configure(font: Path) -> None:
    font_manager.fontManager.addfont(str(font))
    name = font_manager.FontProperties(fname=str(font)).get_name()
    matplotlib.rcParams.update({
        "font.family": name, "font.size": 10.5, "axes.labelcolor": INK,
        "axes.edgecolor": INK, "xtick.color": INK, "ytick.color": INK,
        "text.color": INK, "pdf.fonttype": 42, "ps.fonttype": 42,
        "svg.fonttype": "none", "axes.spines.top": False,
        "axes.spines.right": False,
    })


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, fields: list[str], values: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(values)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def save(fig: plt.Figure, output: Path, stem: str) -> None:
    for suffix in ("pdf", "svg", "png"):
        fig.savefig(output / f"{stem}.{suffix}", bbox_inches="tight",
                    dpi=240 if suffix == "png" else None)
    plt.close(fig)


def style(axis: plt.Axes) -> None:
    axis.grid(True, color=GRID, linewidth=.65, alpha=.8)
    axis.set_axisbelow(True)


def accepted_spectrum_roots(root: Path) -> tuple[list[Path], list[dict]]:
    expected = {
        "source_sha256": "0a2480d10ec14edb71255c635298dc3a242803c99c9c3e0b6c78ca4e56a2e9e2",
        "trace_sha256": "f8af8cb757cd86561695dbcc83e868bb546e2cb08cd7d03f3a53f85cd7cc4fe7",
        "downstream_rate": "50mbit", "base_rtt_ms": 20.0,
        "dualpi2_target": "15ms", "dualpi2_tupdate": "16ms",
        "dualpi2_step": "5ms", "dc_background_mbps": 280.0,
        "transport_queue_slack_bytes": 4096, "deadline_ms": 30000,
        "connections": 2,
    }
    accepted, audit = [], []
    for candidate in sorted(root.glob("qemu-3dgs-split2-l4s-l4s-*")):
        provenance = candidate / "provenance.json"
        if not provenance.is_file():
            continue
        cfg = json.loads(provenance.read_text()).get("configuration", {})
        mismatches = {key: {"actual": cfg.get(key), "expected": value}
                      for key, value in expected.items() if cfg.get(key) != value}
        fractions = cfg.get("l4s_fractions", [])
        valid_fraction = len(fractions) == 1 and float(fractions[0]) in COLORS
        item = {"root": str(candidate), "accepted": not mismatches and valid_fraction,
                "fraction": float(fractions[0]) if valid_fraction else None,
                "mismatches": mismatches, "provenance_sha256": sha256(provenance)}
        audit.append(item)
        if item["accepted"]:
            accepted.append(candidate)
    by_fraction = {float(json.loads((path / "provenance.json").read_text())
                         ["configuration"]["l4s_fractions"][0]): path for path in accepted}
    if set(by_fraction) != set(COLORS):
        raise ValueError(f"spectrum evidence lacks comparable fractions: {sorted(by_fraction)}")
    return [by_fraction[value] for value in sorted(by_fraction)], audit


def rank_release_map(root: Path) -> dict[int, float]:
    values = {}
    for path in root.glob("inputs/*/*-release-ms.txt"):
        for line in path.read_text().splitlines():
            release, rank = line.split()
            values[int(rank)] = float(release)
    return values


def spectrum_records(spectrum_root: Path) -> tuple[dict[int, int], dict[float, list[dict]], list[dict]]:
    roots, audit = accepted_spectrum_roots(spectrum_root)
    manifest = json.loads(next(roots[0].glob("inputs/*/reordering-input.json")).read_text())
    object_bytes = {int(row["importance_rank"]): int(row["payload_bytes"])
                    for row in manifest["objects"]}
    cutoff_count = math.ceil(len(object_bytes) * IMPORTANT_FRACTION)
    important = dict(sorted(object_bytes.items())[:cutoff_count])
    all_runs: dict[float, list[dict]] = defaultdict(list)
    for candidate in roots:
        cfg = json.loads((candidate / "provenance.json").read_text())["configuration"]
        fraction = float(cfg["l4s_fractions"][0])
        releases = rank_release_map(candidate)
        for case in sorted(candidate.glob("l4s-*-rep-*")):
            timeline = case / "combined-arrival-timeline.csv"
            if not timeline.is_file():
                continue
            arrivals = {int(row["importance_rank"]): float(row["experiment_time_us"]) / 1000.0
                        for row in read_csv(timeline)}
            all_runs[fraction].append({"case": case.name, "arrivals": arrivals,
                                       "releases": releases})
    return important, all_runs, audit


def weighted_completion_curve(important: dict[int, int], run: dict,
                              grid: np.ndarray, *, relative: bool) -> np.ndarray:
    total = float(sum(important.values()))
    completed = np.zeros_like(grid, dtype=float)
    for rank, payload in important.items():
        arrival = run["arrivals"].get(rank)
        if arrival is None:
            continue
        value = arrival - run["releases"][rank] if relative else arrival
        completed[grid >= value] += payload
    return completed / total * 100.0


def weighted_quantile(values: list[float], weights: list[int], quantile: float) -> float:
    order = np.argsort(values)
    ordered_values = np.asarray(values)[order]
    ordered_weights = np.asarray(weights, dtype=float)[order]
    threshold = quantile * ordered_weights.sum()
    return float(ordered_values[np.searchsorted(np.cumsum(ordered_weights), threshold)])


def important_latency_statistics(important: dict[int, int], run: dict) -> dict:
    latencies, weights = [], []
    delivered_objects = 0
    delivered_bytes = 0
    for rank, payload in important.items():
        arrival = run["arrivals"].get(rank)
        latency = DEADLINE_MS
        if arrival is not None:
            observed = arrival - run["releases"][rank]
            if observed <= DEADLINE_MS:
                latency = max(observed, 0.0)
                delivered_objects += 1
                delivered_bytes += payload
        latencies.append(latency)
        weights.append(payload)
    return {
        "median_latency_ms": weighted_quantile(latencies, weights, 0.5),
        "p95_latency_ms": weighted_quantile(latencies, weights, 0.95),
        "delivered_objects": delivered_objects,
        "total_objects": len(important),
        "delivered_bytes": delivered_bytes,
        "total_bytes": sum(important.values()),
        "delivered_object_fraction": delivered_objects / len(important),
        "delivered_byte_fraction": delivered_bytes / sum(important.values()),
    }


def plot_12_completion(axis: plt.Axes, grid: np.ndarray,
                       curves_by_fraction: dict[float, np.ndarray], *,
                       release_relative: bool, legend: bool) -> None:
    for fraction in sorted(curves_by_fraction):
        curves = curves_by_fraction[fraction][0 if release_relative else 1]
        median = np.median(curves, axis=0)
        axis.plot(grid / 1000, median, color=COLORS[fraction], linewidth=2.1,
                  label=fa(LABELS[fraction]))
        axis.fill_between(grid / 1000, np.min(curves, axis=0), np.max(curves, axis=0),
                          color=COLORS[fraction], alpha=.12, linewidth=0)
    axis.set_xlabel(fa("زمان از آماده‌شدن شیء (ثانیه)") if release_relative
                    else fa("زمان از شروع بارکاری (ثانیه)"))
    axis.set_ylabel(fa("درصد وزنی بایت‌های مهم تکمیل‌شده (۲۵٪)"))
    axis.set_xlim(0, 30); axis.set_ylim(0, 102); style(axis)
    if legend:
        axis.legend(frameon=False, fontsize=8.3, loc="lower right")


def figure12(spectrum_root: Path, output: Path) -> tuple[dict, list[dict]]:
    important, runs, audit = spectrum_records(spectrum_root)
    grid = np.linspace(0, DEADLINE_MS, 301)
    summary_rows = []
    latency_rows = []
    curves_by_fraction = {}
    fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.1), sharey=True)
    for fraction in sorted(runs):
        latency_curves = np.array([weighted_completion_curve(important, run, grid, relative=True)
                                   for run in runs[fraction]])
        wall_curves = np.array([weighted_completion_curve(important, run, grid, relative=False)
                                for run in runs[fraction]])
        curves_by_fraction[fraction] = (latency_curves, wall_curves)
        for seconds in (5, 10, 30):
            index = int(seconds * 10)
            for repetition, curve in enumerate(latency_curves, 1):
                summary_rows.append({"l4s_fraction": fraction, "repetition": repetition,
                                     "metric": "release_relative_completion_pct",
                                     "time_s": seconds, "important_payload_pct": curve[index]})
        for repetition, run in enumerate(runs[fraction], 1):
            latency_rows.append({"l4s_fraction": fraction, "repetition": repetition,
                                 **important_latency_statistics(important, run)})
    plot_12_completion(axes[0], grid, curves_by_fraction, release_relative=True, legend=False)
    plot_12_completion(axes[1], grid, curves_by_fraction, release_relative=False, legend=True)
    save(fig, output, "figure-12-single-sender-vs-differentiated-fa")
    for stem, release_relative in (("figure-12-a-release-relative-completion-fa", True),
                                   ("figure-12-b-wall-clock-completion-fa", False)):
        panel, axis = plt.subplots(figsize=(6.6, 4.3))
        plot_12_completion(axis, grid, curves_by_fraction,
                           release_relative=release_relative, legend=True)
        save(panel, output, stem)
    write_csv(output / "figure-12-summary.csv", list(summary_rows[0]), summary_rows)
    baseline = {row["repetition"]: row for row in latency_rows if row["l4s_fraction"] == 1.0}
    for row in latency_rows:
        reference = baseline[row["repetition"]]
        row["median_change_vs_single_l4s_ms"] = (
            row["median_latency_ms"] - reference["median_latency_ms"])
        row["p95_change_vs_single_l4s_ms"] = (
            row["p95_latency_ms"] - reference["p95_latency_ms"])
    write_csv(output / "figure-12-latency-statistics.csv", list(latency_rows[0]), latency_rows)
    paired = {}
    for fraction in sorted(runs):
        rows = [row for row in latency_rows if row["l4s_fraction"] == fraction]
        paired[str(fraction)] = {
            "median_change_vs_single_l4s_ms": float(np.median(
                [row["median_change_vs_single_l4s_ms"] for row in rows])),
            "p95_change_vs_single_l4s_ms": float(np.median(
                [row["p95_change_vs_single_l4s_ms"] for row in rows])),
            "median_delivered_object_fraction": float(np.median(
                [row["delivered_object_fraction"] for row in rows])),
            "median_delivered_byte_fraction": float(np.median(
                [row["delivered_byte_fraction"] for row in rows])),
        }
    return ({"claim_type": "measured", "important_objects": len(important),
             "important_payload_bytes": sum(important.values()),
             "repetitions_per_fraction": {str(k): len(v) for k, v in runs.items()},
             "meets_planned_three_repetitions": all(len(value) >= 3 for value in runs.values()),
             "censoring_deadline_ms": DEADLINE_MS,
             "latency_change_sign": "scheme_minus_single_l4s; positive_is_slower",
             "paired_latency_and_coverage": paired,
             "summary_rows": summary_rows}, audit)


def bound_ms(initial_bdp: np.ndarray, lead_rtt: np.ndarray) -> np.ndarray:
    return RTT_MS * np.maximum(initial_bdp - lead_rtt, 0.0)


def figure13(output: Path) -> dict:
    backlog = np.linspace(0, CONTROL_MAX, 81)
    gain = bound_ms(backlog, np.zeros_like(backlog))
    fig, ax = plt.subplots(figsize=(6.8, 4.2))
    ax.plot(backlog, gain, color="#1b7f5b", linewidth=2.5)
    ax.fill_between(backlog, 0, gain, color="#1b7f5b", alpha=.13)
    ax.set_xlabel(fa("باقی‌مانده داده کم‌اولویت، B / BDP"))
    ax.set_ylabel(fa("بیشینه صرفه‌جویی ممکن (میلی‌ثانیه)"))
    ax.text(.98, .06, fa("حد تحلیلی؛ نه اندازه‌گیری شبکه"), transform=ax.transAxes,
            ha="right", fontsize=9, color="#8b3a3a")
    ax.set_xlim(0, 4); ax.set_ylim(0, 84); style(ax)
    save(fig, output, "figure-13-gain-vs-residual-backlog-fa")
    values = [{"backlog_bdp": float(value), "upper_bound_gain_ms": float(bound)}
              for value, bound in zip(backlog, gain)]
    write_csv(output / "figure-13-summary.csv", list(values[0]), values)
    return {"claim_type": "analytical_counterfactual_upper_bound",
            "formula": "gain_max_ms = RTT_ms * B_over_BDP", "rtt_ms": RTT_MS}


def applicability_surface(points: int = 161) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    initial = np.linspace(0, CONTROL_MAX, points)
    lead = np.linspace(0, CONTROL_MAX, points)
    xx, yy = np.meshgrid(initial, lead)
    return xx, yy, bound_ms(xx, yy)


def draw_heatmap(axis: plt.Axes, *, colorbar: bool, fig: plt.Figure):
    xx, yy, gain = applicability_surface()
    image = axis.pcolormesh(xx, yy, gain, shading="auto", cmap="YlGnBu", vmin=0, vmax=80)
    axis.plot([0, 4], [0, 4], color="white", linestyle=":", linewidth=1.3,
              label=fa("مرز تخلیه کامل"))
    axis.set_xlim(0, 4); axis.set_ylim(0, 4)
    axis.set_xlabel(fa("داده کم‌اولویت هنگام ارسال") + "  B0 / BDP")
    axis.set_ylabel(fa("فاصله تا تغییر اولویت") + "  dt / RTT")
    if colorbar:
        fig.colorbar(image, ax=axis, label=fa("حد بالای صرفه‌جویی (میلی‌ثانیه)"))
    return image


def figure14(output: Path) -> dict:
    fig, ax = plt.subplots(figsize=(6.6, 5.0))
    draw_heatmap(ax, colorbar=True, fig=fig)
    ax.legend(frameon=False, loc="upper left")
    ax.text(.98, .04, fa("مدل تخلیه با نرخ گلوگاه"), transform=ax.transAxes,
            ha="right", color="white", fontsize=9)
    save(fig, output, "figure-14-applicability-region-fa")
    return {"claim_type": "analytical_counterfactual_upper_bound",
            "formula": "gain_max_ms = RTT_ms * max(B0_over_BDP - delta_t_over_RTT, 0)",
            "domain": {"initial_backlog_bdp": [0, 4], "lead_rtt": [0, 4]}}


def pair_roots(root: Path) -> list[Path]:
    names = (
        "qemu-thesis-3dgs-pair-rep-01-final-20260825T164914Z",
        "qemu-thesis-3dgs-pair-rep-02-rerun3-final-20260825T182350Z",
        "qemu-thesis-3dgs-pair-rep-03-rerun-final-20260825T195325Z",
    )
    paths = [root / name for name in names]
    if not all(path.is_dir() for path in paths):
        raise ValueError("one or more final 3DGS pair roots are missing")
    return paths


def workload_event_rows(root: Path) -> tuple[list[dict], list[dict]]:
    repetitions, provenance = [], []
    for repetition, pair in enumerate(pair_roots(root), 1):
        mode = pair / "l4s"
        case = next(mode.glob("l4s-1-rep-*"))
        frozen = json.loads((mode / "inputs/frozen-demand-order.json").read_text())
        manifest = json.loads((mode / "inputs/l4s-1/reordering-input.json").read_text())
        object_map = {}
        for row in manifest["objects"]:
            key = (row["track_id"], str(row["group_id"]), int(row["subgroup_id"]), int(row["object_id"]))
            object_map[key] = {"rank": int(row["importance_rank"]),
                               "payload_bytes": int(row["payload_bytes"])}
        release = rank_release_map(mode)
        admissions = read_csv(case / "high-prague/admission-order.csv")
        arrivals = {int(row["bundle_record_index"]): float(row["arrival_time_us"])
                    for row in read_csv(case / "high-prague/arrival-timeline.csv")}
        for event in frozen["events"]:
            raw_key = event["object_key"]
            key = (raw_key[0], str(raw_key[1]), int(raw_key[2]), int(raw_key[3]))
            meta = object_map[key]
            event_us = release[meta["rank"]] * 1000.0
            harmful = []
            for admitted in admissions:
                if int(admitted["importance_rank"]) <= meta["rank"]:
                    continue
                admitted_us = float(admitted["time_us"])
                if admitted_us > event_us:
                    continue
                completion_us = arrivals.get(int(admitted["bundle_record_index"]), float("inf"))
                if completion_us > event_us:
                    harmful.append((int(admitted["payload_bytes"]), event_us - admitted_us))
            residual = sum(payload for payload, _ in harmful)
            age_us = (sum(payload * age for payload, age in harmful) / residual
                      if residual else 0.0)
            repetitions.append({
                "event_key": "|".join(map(str, raw_key)), "rank": meta["rank"],
                "payload_bytes": meta["payload_bytes"], "release_ms": release[meta["rank"]],
                "repetition": repetition, "residual_bytes": residual,
                "residual_bdp": residual / BDP_BYTES, "age_rtt": age_us / (RTT_MS * 1000.0),
            })
        prov = mode / "provenance.json"
        provenance.append({"root": str(pair), "provenance_sha256": sha256(prov),
                           "frozen_demand_sha256": sha256(mode / "inputs/frozen-demand-order.json")})
    return repetitions, provenance


def aggregate_events(values: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in values:
        grouped[(row["event_key"], row["rank"], row["release_ms"])].append(row)
    output = []
    for key, group in grouped.items():
        residual = float(np.median([row["residual_bdp"] for row in group]))
        age = float(np.median([row["age_rtt"] for row in group]))
        inferred_initial = residual + age
        output.append({
            "event_key": key[0], "rank": key[1], "release_ms": key[2],
            "payload_bytes": group[0]["payload_bytes"], "repetitions": len(group),
            "residual_bdp_median": residual, "age_rtt_median": age,
            "inferred_initial_bdp": inferred_initial,
            "inside_control_grid": inferred_initial <= CONTROL_MAX and age <= CONTROL_MAX,
            "positive_theoretical_gain": inferred_initial > age,
            "upper_bound_gain_ms": RTT_MS * max(inferred_initial - age, 0.0),
        })
    return sorted(output, key=lambda row: (row["release_ms"], row["rank"]))


def plot_15_applicability(axis: plt.Axes, fig: plt.Figure, inside: list[dict]) -> None:
    draw_heatmap(axis, colorbar=True, fig=fig)
    if inside:
        axis.scatter([row["inferred_initial_bdp"] for row in inside],
                     [row["age_rtt_median"] for row in inside],
                     s=10, facecolors="none", edgecolors="#d94f45", linewidths=.7,
                     alpha=.65, label=fa("رخدادهای واقعی داخل شبکه"))
    axis.legend(frameon=False, fontsize=8, loc="upper left")


def plot_15_residual(axis: plt.Axes, positive: np.ndarray) -> None:
    if len(positive):
        ordered = np.sort(positive)
        axis.plot(ordered, np.arange(1, len(ordered) + 1) / len(ordered),
                  color="#b44b4b", linewidth=2.2)
    axis.set_xscale("log")
    axis.set_xlabel(fa("باقی‌مانده واقعی در لحظه تغییر، B / BDP"))
    axis.set_ylabel(fa("CDF رخدادهای دارای باقی‌مانده")); style(axis)
    axis.axvline(CONTROL_MAX, color="#1976a3", linestyle=":", linewidth=1.2,
                 label=fa("مرز شبکه کنترل‌شده"))
    axis.legend(frameon=False, fontsize=8, loc="lower right")


def figure15(pair_root: Path, output: Path) -> tuple[dict, list[dict]]:
    raw, provenance = workload_event_rows(pair_root)
    events = aggregate_events(raw)
    inside = [row for row in events if row["inside_control_grid"]]
    outside = [row for row in events if not row["inside_control_grid"]]
    zero = [row for row in events if row["residual_bdp_median"] == 0]
    positive = np.array([row["residual_bdp_median"] for row in events
                         if row["residual_bdp_median"] > 0])
    fig, axes = plt.subplots(1, 2, figsize=(10.3, 4.3))
    plot_15_applicability(axes[0], fig, inside); plot_15_residual(axes[1], positive)
    save(fig, output, "figure-15-real-3dgs-applicability-fa")
    panel, axis = plt.subplots(figsize=(6.4, 4.8)); plot_15_applicability(axis, panel, inside)
    save(panel, output, "figure-15-a-applicability-fa")
    panel, axis = plt.subplots(figsize=(6.4, 4.3)); plot_15_residual(axis, positive)
    save(panel, output, "figure-15-b-residual-backlog-cdf-fa")
    write_csv(output / "figure-15-events.csv", list(events[0]), events)
    total_payload = sum(row["payload_bytes"] for row in events)
    inside_payload = sum(row["payload_bytes"] for row in inside)
    serialization_ms = 57_407 * 8 / RATE_BPS * 1000.0
    threshold_fractions = {}
    for name, threshold in (("gt_0_ms", 0.0),
                            ("gt_1_ms", 1.0),
                            ("gt_one_object_serialization", serialization_ms)):
        selected = [row for row in inside if row["upper_bound_gain_ms"] > threshold]
        threshold_fractions[name] = {
            "threshold_ms": threshold,
            "events": len(selected),
            "event_fraction": len(selected) / len(events),
            "payload_fraction": (
                sum(row["payload_bytes"] for row in selected) / total_payload),
            "domain_restriction": "inside Figure 14 controlled 0--4 BDP/RTT region",
        }
    return ({
        "claim_type": "measured_workload_state_over_analytical_bound",
        "unique_events": len(events), "raw_repetition_events": len(raw),
        "events_zero_residual": len(zero), "events_inside_control_grid": len(inside),
        "events_outside_control_grid": len(outside),
        "event_fraction_inside_control_grid": len(inside) / len(events),
        "payload_fraction_inside_control_grid": inside_payload / total_payload,
        "positive_residual_percentiles_bdp": ({
            str(p): float(np.percentile(positive, p)) for p in (50, 90, 95, 99)
        } if len(positive) else {}),
        "analytical_upper_bound_threshold_fractions": threshold_fractions,
        "mapping_caveat": "B0/BDP is inferred as measured residual B/BDP plus byte-weighted age/RTT under bottleneck-rate drainage",
    }, provenance)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spectrum-root", type=Path, required=True)
    parser.add_argument("--pair-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=Path("fonts/XB Niloofar.ttf"))
    parser.add_argument("--write-captions", action="store_true",
                        help="write captions-fa.md; disabled for plot-only regeneration")
    args = parser.parse_args()
    if not args.font.is_file():
        raise SystemExit(f"missing Persian font: {args.font}")
    args.output.mkdir(parents=True, exist_ok=True)
    configure(args.font)
    fig, axis = plt.subplots(figsize=(9.2, 2.5)); draw_spectrum_topology(axis, fa)
    save(fig, args.output, "topology-for-figure-12-fa")
    fig, axis = plt.subplots(figsize=(9.2, 2.7))
    draw_pair_topology(axis, fa, show_classic=False)
    save(fig, args.output, "topology-for-figure-15-fa")
    fig12, spectrum_audit = figure12(args.spectrum_root, args.output)
    fig13 = figure13(args.output)
    fig14 = figure14(args.output)
    fig15, pair_provenance = figure15(args.pair_root, args.output)
    summary = {
        "schema_version": 2, "figure_12": fig12, "figure_13": fig13,
        "figure_14": fig14, "figure_15": fig15,
        "constants": {"rate_bps": RATE_BPS, "rtt_ms": RTT_MS,
                      "bdp_bytes": BDP_BYTES, "control_max_bdp_rtt": CONTROL_MAX},
        "provenance": {"spectrum_audit": spectrum_audit,
                       "three_dgs_pairs": pair_provenance},
    }
    (args.output / "figures-12-15-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if args.write_captions:
        (args.output / "captions-fa.md").write_text(
            "# شرح شکل‌های ۱۲ تا ۱۵\n\n"
            "- **توپولوژی شکل ۱۲:** این توپولوژی فقط در شکل ۱۲ استفاده شده است. میزبان‌ها، دو سوئیچ، مسیرهای 3DGS و پس‌زمینه Reno، کنترل ازدحام، نرخ‌های ۳۰۰ و ۵۰ مگابیت‌برثانیه و RTT برابر ۲۰ میلی‌ثانیه نشان داده شده‌اند.\n"
            "- **شکل ۱۲:** درصد ثابت و یکسانی از بایت‌های مهم برای همه روش‌ها دنبال می‌شود؛ اشیای نرسیده تا ۳۰ ثانیه سانسور می‌شوند و حذف نمی‌شوند. خط، میانه دو تکرار و ناحیه، بازه آن‌هاست.\n"
            "- **شکل ۱۳:** حد بالای پادواقعی صرفه‌جویی اگر اثر رقابتی B کاملاً حذف شود؛ این منحنی تحلیلی است و توپولوژی یا کنترل ازدحام تجربی ندارد.\n"
            "- **شکل ۱۴:** حد بالای تحلیلی در فضای B₀ و Δt با فرض تخلیه در نرخ گلوگاه؛ این نگاشت اندازه‌گیری شبکه نیست و توپولوژی تجربی ندارد.\n"
            "- **توپولوژی شکل ۱۵:** این توپولوژی فقط در شکل ۱۵ استفاده شده است. مسیر دوسوئیچ Prague/ECT(1)، دو جریان Cubic و نرخ‌های فعال ۳۰۰ و ۱۰۰ مگابیت‌برثانیه نشان داده شده‌اند.\n"
            "- **شکل ۱۵:** رخدادهای واقعی 3DGS پس از تجمیع میانه سه تکرار روی ناحیه تحلیلی قرار گرفته‌اند و پنل دوم توزیع باقی‌مانده واقعی را نشان می‌دهد.\n")
    verification = {
        "all_four_figures_present": all(
            (args.output / f"figure-{number:02d}-{stem}.pdf").is_file()
            for number, stem in ((12, "single-sender-vs-differentiated-fa"),
                                 (13, "gain-vs-residual-backlog-fa"),
                                 (14, "applicability-region-fa"),
                                 (15, "real-3dgs-applicability-fa"))),
        "all_topology_figures_present": all(
            (args.output / name).is_file()
            for name in ("topology-for-figure-12-fa.pdf",
                         "topology-for-figure-15-fa.pdf")),
        "all_split_panel_figures_present": all(
            (args.output / name).is_file()
            for name in ("figure-12-a-release-relative-completion-fa.pdf",
                         "figure-12-b-wall-clock-completion-fa.pdf",
                         "figure-15-a-applicability-fa.pdf",
                         "figure-15-b-residual-backlog-cdf-fa.pdf")),
        "figure_12_fixed_denominator": True,
        "figure_12_meets_planned_three_repetitions": fig12["meets_planned_three_repetitions"],
        "figure_13_14_claim_type": "analytical_counterfactual_upper_bound",
        "figure_15_unique_events": fig15["unique_events"],
        "spectrum_roots_accepted": sum(row["accepted"] for row in spectrum_audit),
        "persian_shaping": True,
    }
    (args.output / "verification-summary.json").write_text(json.dumps(verification, indent=2) + "\n")
    artifacts = {path.name: sha256(path) for path in sorted(args.output.iterdir())
                 if path.is_file() and path.name != "figures-12-15-manifest.json"}
    (args.output / "figures-12-15-manifest.json").write_text(
        json.dumps({"artifacts": artifacts, "verification": verification}, indent=2) + "\n")


if __name__ == "__main__":
    main()
