#!/usr/bin/env python3
"""Create Persian thesis Figures 6--10 and their numerical summaries."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
import matplotlib
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
import numpy as np

try:
    from thesis_topology import draw_coexistence_topology, draw_pair_topology
except ModuleNotFoundError:
    from tools.l4s.thesis_topology import draw_coexistence_topology, draw_pair_topology


PCTS = (50.0, 95.0, 99.0, 99.9)
CLASSIC = "#b44b4b"
L4S = "#1976a3"
CUBIC = "#d69b2d"
INK = "#28323c"
GRID = "#d8dde3"


def fa(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def configure(font: Path) -> None:
    font_manager.fontManager.addfont(str(font))
    name = font_manager.FontProperties(fname=str(font)).get_name()
    matplotlib.rcParams.update({
        "font.family": name,
        "font.size": 11,
        "axes.labelcolor": INK,
        "axes.edgecolor": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "text.color": INK,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def save_figure(fig: plt.Figure, output: Path, stem: str) -> None:
    for suffix in ("pdf", "svg", "png"):
        kwargs = {"dpi": 220} if suffix == "png" else {}
        fig.savefig(output / f"{stem}.{suffix}", bbox_inches="tight", **kwargs)
    plt.close(fig)


def style_axis(ax: plt.Axes) -> None:
    ax.grid(True, color=GRID, linewidth=0.65, alpha=0.8)
    ax.set_axisbelow(True)


def bar_legend(ax: plt.Axes, *, ncol: int = 2) -> None:
    """Place a bar-chart legend above its axes, never over measured bars."""
    ax.legend(frameon=False, fontsize=8.5, ncol=ncol, loc="lower center",
              bbox_to_anchor=(.5, 1.02), borderaxespad=0.)


def read_float_column(path: Path, name: str) -> np.ndarray:
    with path.open(newline="") as stream:
        return np.array([float(row[name]) for row in csv.DictReader(stream)])


def ecdf(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ordered = np.sort(values)
    return ordered, np.arange(1, len(ordered) + 1) / len(ordered)


def completion_cdf(values: np.ndarray, denominator: int) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative completions on a common delivered-object denominator."""
    if denominator < len(values):
        raise ValueError("completion denominator must cover every delivered object")
    ordered = np.sort(values)
    return ordered, np.arange(1, len(ordered) + 1) / denominator


def percentile_summary(values: np.ndarray) -> dict[str, float]:
    return {str(pct): float(np.percentile(values, pct)) for pct in PCTS}


def mark_percentiles(ax: plt.Axes, values: np.ndarray, color: str,
                     upper: bool, *, denominator: int | None = None) -> None:
    denominator = len(values) if denominator is None else denominator
    for index, pct in enumerate(PCTS):
        x = float(np.percentile(values, pct))
        y = pct / 100.0 * len(values) / denominator
        ax.scatter([x], [y], s=24, color=color, zorder=5)
        offset = 7 if upper else -13
        ax.annotate(f"p{pct:g}", (x, y), xytext=(3, offset),
                    textcoords="offset points", fontsize=7.5, color=color)


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def network_cases(root: Path) -> list[dict]:
    rows = []
    for path in sorted(root.glob("*-tcp-cubic-flows-*-rep-*/case-summary.json")):
        data = json.loads(path.read_text())
        data["path"] = str(path.parent)
        rows.append(data)
    return rows


def figure_06(network: Path, output: Path, summary: dict) -> None:
    values = {}
    for mode in ("reno", "prague"):
        arrays = [read_float_column(path, "queue_delay_ms") for path in sorted(
            network.glob(f"{mode}-tcp-cubic-flows-01-rep-*/queue-delay.csv"))]
        values[mode] = np.concatenate(arrays)
    fig, ax = plt.subplots(figsize=(7.0, 4.3))
    for mode, color, label in (("reno", CLASSIC, "Classic: Reno / Not-ECT"),
                               ("prague", L4S, "L4S: Prague / ECT(1)")):
        x, y = ecdf(values[mode]); ax.plot(x, y, lw=2.2, color=color, label=fa(label))
        mark_percentiles(ax, values[mode], color, upper=mode == "prague")
    ax.set_xlabel(fa("تاخیر صف (میلی‌ثانیه)")); ax.set_ylabel(fa("تابع توزیع تجمعی تجربی"))
    ax.set_ylim(0, 1.01); style_axis(ax); ax.legend(frameon=False, loc="lower right")
    save_figure(fig, output, "figure-06-queue-delay-cdf-fa")
    summary["figure_06"] = {mode: {"samples": len(array), "percentiles_ms": percentile_summary(array)}
                            for mode, array in values.items()}
    rows = [{"mode": mode, "percentile": pct, "queue_delay_ms": np.percentile(array, pct)}
            for mode, array in values.items() for pct in PCTS]
    write_csv(output / "figure-06-summary.csv", list(rows[0]), rows)


def binned_queue(case: Path, width: float = 0.25) -> tuple[np.ndarray, np.ndarray]:
    with (case / "queue-delay.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    time = np.array([float(row["measurement_time_s"]) for row in rows])
    delay = np.array([float(row["queue_delay_ms"]) for row in rows])
    centers = np.arange(width / 2, 8.0, width)
    medians = np.array([np.median(delay[(time >= c - width / 2) & (time < c + width / 2)])
                        for c in centers])
    return centers, medians


def figure_07_data(cases: list[dict]) -> dict[str, object]:
    selected = [row for row in cases if row["background_flow_count"] == 1]
    grouped = {mode: [row for row in selected if row["mode"] == mode]
               for mode in ("reno", "prague")}
    x = np.arange(2); width = .32
    foreground = [np.mean([r["foreground_mbps"] for r in grouped[m]]) for m in ("reno", "prague")]
    cubic = [np.mean([r["cubic_aggregate_mbps"] for r in grouped[m]]) for m in ("reno", "prague")]
    utilization = [np.mean([r["utilization_percent"] for r in grouped[m]]) for m in ("reno", "prague")]
    fairness = [np.mean([r["jain_fairness"] for r in grouped[m]]) * 100 for m in ("reno", "prague")]
    utilization_low = [min(r["utilization_percent"] for r in grouped[m]) for m in ("reno", "prague")]
    utilization_high = [max(r["utilization_percent"] for r in grouped[m]) for m in ("reno", "prague")]
    fairness_low = [min(r["jain_fairness"] for r in grouped[m]) * 100 for m in ("reno", "prague")]
    fairness_high = [max(r["jain_fairness"] for r in grouped[m]) * 100 for m in ("reno", "prague")]
    queue = {}
    for mode in ("reno", "prague"):
        series = [binned_queue(Path(row["path"])) for row in grouped[mode]]
        times = series[0][0]; matrix = np.vstack([item[1] for item in series])
        queue[mode] = {"times": times, "mean": np.nanmean(matrix, axis=0),
                       "low": np.nanmin(matrix, axis=0), "high": np.nanmax(matrix, axis=0)}
    return {"grouped": grouped, "x": x, "width": width, "foreground": foreground,
            "cubic": cubic, "utilization": utilization, "fairness": fairness,
            "utilization_low": utilization_low, "utilization_high": utilization_high,
            "fairness_low": fairness_low, "fairness_high": fairness_high, "queue": queue}


def plot_07_throughput(ax: plt.Axes, data: dict[str, object]) -> None:
    x, width = data["x"], data["width"]
    # Separate containers keep each foreground colour identifiable in the legend.
    ax.bar(x[0] - width / 2, data["foreground"][0], width, color=CLASSIC,
           label="Classic MoQ: Reno / Not-ECT")
    ax.bar(x[1] - width / 2, data["foreground"][1], width, color=L4S,
           label="L4S MoQ: Prague / ECT(1)")
    ax.bar(x + width / 2, data["cubic"], width, color=CUBIC,
           label="Background: TCP Cubic / Not-ECT")
    ax.set_xticks(x, ["Classic: Reno / Not-ECT", "L4S: Prague / ECT(1)"], fontsize=8)
    ax.set_xlabel(fa("حالت جریان MoQ در اندازه‌گیری هم‌زیستی"))
    ax.set_ylabel(fa("گذردهی (مگابیت‌برثانیه)")); style_axis(ax); bar_legend(ax)


def plot_07_utilization_fairness(ax: plt.Axes, data: dict[str, object]) -> None:
    x, width = data["x"], data["width"]
    utilization, fairness = data["utilization"], data["fairness"]
    ax.bar(x - width / 2, utilization, width, color="#4a7d44", label=fa("بهره‌برداری"),
           yerr=[np.asarray(utilization) - data["utilization_low"],
                 np.asarray(data["utilization_high"]) - utilization], capsize=3)
    ax.bar(x + width / 2, fairness, width, color="#7650a5", label=fa("عدالت جین"),
           yerr=[np.asarray(fairness) - data["fairness_low"],
                 np.asarray(data["fairness_high"]) - fairness], capsize=3)
    ax.set_xticks(x, ["Classic: Reno / Not-ECT", "L4S: Prague / ECT(1)"], fontsize=8)
    ax.set_xlabel(fa("حالت جریان MoQ در اندازه‌گیری هم‌زیستی"))
    ax.set_ylabel(fa("درصد")); ax.set_ylim(0, 105); style_axis(ax); bar_legend(ax)


def plot_07_queue(ax: plt.Axes, data: dict[str, object]) -> None:
    queue = data["queue"]
    for mode, color, label in (("reno", CLASSIC, "Classic: Reno / Not-ECT"),
                               ("prague", L4S, "L4S: Prague / ECT(1)")):
        row = queue[mode]
        ax.plot(row["times"], row["mean"], color=color, lw=2, label=label)
        ax.fill_between(row["times"], row["low"], row["high"], color=color,
                        alpha=.16, linewidth=0)
    ax.set_xlabel(fa("زمان از آغاز پنجرهٔ اندازه‌گیری (ثانیه؛ پنجرهٔ ۸ ثانیه‌ای)"))
    ax.set_ylabel(fa("میانه تأخیر صف در بازه ۲۵۰ میلی‌ثانیه"))
    ax.legend(frameon=False, fontsize=8, loc="upper right"); style_axis(ax)


def figure_07(network: Path, output: Path, cases: list[dict], summary: dict) -> None:
    data = figure_07_data(cases)
    fig = plt.figure(figsize=(8.4, 7.4))
    grid = fig.add_gridspec(2, 2, height_ratios=(1, 1.25), hspace=.54, wspace=.34)
    ax_t = fig.add_subplot(grid[0, 0]); ax_f = fig.add_subplot(grid[0, 1]); ax_q = fig.add_subplot(grid[1, :])
    plot_07_throughput(ax_t, data); plot_07_utilization_fairness(ax_f, data); plot_07_queue(ax_q, data)
    save_figure(fig, output, "figure-07-prague-cubic-coexistence-fa")
    for stem, plotter, size in (("figure-07-a-throughput-fa", plot_07_throughput, (5.8, 4.3)),
                                ("figure-07-b-utilization-fairness-fa", plot_07_utilization_fairness, (5.8, 4.3)),
                                ("figure-07-c-queue-delay-fa", plot_07_queue, (7.0, 4.3))):
        panel, axis = plt.subplots(figsize=size); plotter(axis, data); panel.subplots_adjust(top=.78)
        save_figure(panel, output, stem)
    summary["figure_07"] = {
        "mean_foreground_mbps": dict(zip(("reno", "prague"), data["foreground"])),
        "mean_cubic_mbps": dict(zip(("reno", "prague"), data["cubic"])),
        "mean_utilization_percent": dict(zip(("reno", "prague"), data["utilization"])),
        "mean_jain_fairness": {m: data["fairness"][i] / 100 for i, m in enumerate(("reno", "prague"))},
        "queue_timeseries": {mode: {"time_s": row["times"].tolist(),
                                     "mean_queue_delay_ms": row["mean"].tolist()}
                                 for mode, row in data["queue"].items()},
    }
    rows = []
    for mode in ("reno", "prague"):
        for row in data["grouped"][mode]:
            rows.append({"mode": mode, "repetition": row["repetition"],
                         "foreground_mbps": row["foreground_mbps"],
                         "cubic_mbps": row["cubic_aggregate_mbps"],
                         "utilization_percent": row["utilization_percent"],
                         "jain_fairness": row["jain_fairness"]})
    write_csv(output / "figure-07-summary.csv", list(rows[0]), rows)


def figure_08(output: Path, cases: list[dict], summary: dict) -> None:
    counts = (0, 1, 2, 4)
    fig, (ax_t, ax_q) = plt.subplots(
        1, 2, figsize=(8.5, 4.0), gridspec_kw={"wspace": .32})
    output_rows = []
    trade = {}
    for mode, color, label, marker in (("reno", CLASSIC, "کلاسیک", "o"),
                                       ("prague", L4S, "L4S", "s")):
        util_means=[]; p95_means=[]; p99_means=[]; util_low=[]; util_high=[]; p95_low=[]; p95_high=[]; p99_low=[]; p99_high=[]
        foreground_means=[]; cubic_means=[]
        for count in counts:
            rows = [r for r in cases if r["mode"] == mode and r["background_flow_count"] == count]
            util=np.array([r["utilization_percent"] for r in rows]); p95=np.array([r["queue"]["percentiles_ms"]["95.0"] for r in rows]); p99=np.array([r["queue"]["percentiles_ms"]["99.0"] for r in rows])
            foreground_means.append(np.mean([r["foreground_mbps"] for r in rows]))
            cubic_means.append(np.mean([r["cubic_aggregate_mbps"] for r in rows]))
            util_means.append(util.mean()); util_low.append(util.min()); util_high.append(util.max())
            p95_means.append(p95.mean()); p95_low.append(p95.min()); p95_high.append(p95.max())
            p99_means.append(p99.mean()); p99_low.append(p99.min()); p99_high.append(p99.max())
            for r in rows:
                output_rows.append({"mode": mode, "cubic_flows": count, "repetition": r["repetition"],
                                    "utilization_percent": r["utilization_percent"],
                                    "queue_p95_ms": r["queue"]["percentiles_ms"]["95.0"],
                                    "queue_p99_ms": r["queue"]["percentiles_ms"]["99.0"]})
        ax_q.errorbar(counts, p95_means, yerr=[np.array(p95_means)-p95_low, np.array(p95_high)-p95_means], color=color, marker=marker, capsize=3, lw=2, label=f"{fa(label)} - p95")
        ax_q.errorbar(counts, p99_means, yerr=[np.array(p99_means)-p99_low, np.array(p99_high)-p99_means], color=color, marker=marker, capsize=3, lw=1.5, ls=":", label=f"{fa(label)} - p99")
        trade[mode] = {"counts": counts, "utilization_mean": util_means,
                       "foreground_mean_mbps": foreground_means,
                       "cubic_aggregate_mean_mbps": cubic_means,
                       "p95_mean_ms": p95_means, "p95_low_ms": p95_low,
                       "p95_high_ms": p95_high, "p99_mean_ms": p99_means,
                       "p99_low_ms": p99_low, "p99_high_ms": p99_high}
    # Each mode is a separate matched experiment: stack its foreground and
    # aggregate Cubic throughput, rather than adding Classic and L4S together.
    x = np.arange(len(counts)); width = .34
    for offset, mode, color, label in ((-width / 2, "reno", CLASSIC, "Classic MoQ: Reno / Not-ECT"),
                                       (width / 2, "prague", L4S, "L4S MoQ: Prague / ECT(1)")):
        foreground = np.asarray(trade[mode]["foreground_mean_mbps"])
        cubic = np.asarray(trade[mode]["cubic_aggregate_mean_mbps"])
        # Normalize each matched stack to the configured 20 Mbit/s bottleneck.
        # This removes small wire/payload accounting overshoots from the visual
        # capacity comparison while preserving the measured composition.
        total = foreground + cubic
        scale = np.divide(20.0, total, out=np.ones_like(total), where=total > 0)
        foreground_plot = foreground * scale
        cubic_plot = cubic * scale
        ax_t.bar(x + offset, foreground_plot, width, color=color, label=label)
        ax_t.bar(x + offset, cubic_plot, width, bottom=foreground_plot, color=CUBIC,
                 edgecolor=color, linewidth=1.0, alpha=.92,
                 label="Background: TCP Cubic / Not-ECT" if mode == "reno" else "_nolegend_")
    ax_t.set_xticks(x, counts); ax_t.set_xlabel(fa("تعداد جریان‌های Cubic"))
    ax_t.set_ylabel(fa("گذردهی نرمال‌شده (مگابیت‌برثانیه)")); ax_t.set_ylim(0, 20.5)
    style_axis(ax_t); bar_legend(ax_t, ncol=3)
    ax_q.set_xticks(counts); ax_q.set_xlabel(fa("تعداد جریان‌های Cubic")); style_axis(ax_q)
    ax_q.set_ylabel(fa("تاخیر صف (میلی‌ثانیه)")); ax_q.legend(frameon=False, fontsize=8)
    fig.subplots_adjust(top=.80)
    save_figure(fig, output, "figure-08-throughput-latency-tradeoff-fa")
    panel, axis = plt.subplots(figsize=(6.3, 4.4)); plot_08_throughput(axis, trade, counts)
    panel.subplots_adjust(top=.78); save_figure(panel, output, "figure-08-a-throughput-fa")
    panel, axis = plt.subplots(figsize=(6.3, 4.4)); plot_08_queue_delay(axis, trade, counts)
    save_figure(panel, output, "figure-08-b-queue-delay-fa")
    summary["figure_08"] = trade
    write_csv(output / "figure-08-summary.csv", list(output_rows[0]), output_rows)


def plot_08_throughput(ax: plt.Axes, trade: dict[str, dict], counts: tuple[int, ...]) -> None:
    x = np.arange(len(counts)); width = .34
    for offset, mode, color, label in ((-width / 2, "reno", CLASSIC, "Classic MoQ: Reno / Not-ECT"),
                                       (width / 2, "prague", L4S, "L4S MoQ: Prague / ECT(1)")):
        foreground = np.asarray(trade[mode]["foreground_mean_mbps"])
        cubic = np.asarray(trade[mode]["cubic_aggregate_mean_mbps"])
        total = foreground + cubic
        scale = np.divide(20.0, total, out=np.ones_like(total), where=total > 0)
        foreground_plot = foreground * scale
        cubic_plot = cubic * scale
        ax.bar(x + offset, foreground_plot, width, color=color, label=label)
        ax.bar(x + offset, cubic_plot, width, bottom=foreground_plot, color=CUBIC,
               edgecolor=color, linewidth=1.0, alpha=.92,
               label="Background: TCP Cubic / Not-ECT" if mode == "reno" else "_nolegend_")
    ax.set_xticks(x, counts); ax.set_xlabel(fa("تعداد جریان‌های Cubic"))
    ax.set_ylabel(fa("گذردهی نرمال‌شده (مگابیت‌برثانیه)")); ax.set_ylim(0, 20.5)
    style_axis(ax); bar_legend(ax, ncol=3)


def plot_08_queue_delay(ax: plt.Axes, trade: dict[str, dict], counts: tuple[int, ...]) -> None:
    for mode, color, label, marker in (("reno", CLASSIC, "Classic: Reno / Not-ECT", "o"),
                                       ("prague", L4S, "L4S: Prague / ECT(1)", "s")):
        p95 = np.asarray(trade[mode]["p95_mean_ms"])
        p99 = np.asarray(trade[mode]["p99_mean_ms"])
        ax.errorbar(counts, p95,
                    yerr=[p95 - trade[mode]["p95_low_ms"],
                          np.asarray(trade[mode]["p95_high_ms"]) - p95],
                    color=color, marker=marker, capsize=3, lw=2, label=f"{label} - p95")
        ax.errorbar(counts, p99,
                    yerr=[p99 - trade[mode]["p99_low_ms"],
                          np.asarray(trade[mode]["p99_high_ms"]) - p99],
                    color=color, marker=marker, capsize=3, lw=1.5, ls=":",
                    label=f"{label} - p99")
    ax.set_xticks(counts); ax.set_xlabel(fa("تعداد جریان‌های Cubic"))
    ax.set_ylabel(fa("تأخیر صف (میلی‌ثانیه)")); ax.legend(frameon=False, fontsize=7.4)
    style_axis(ax)


def pair_roots(root: Path) -> list[Path]:
    names = (
        "qemu-thesis-3dgs-pair-rep-01-final-20260825T164914Z",
        "qemu-thesis-3dgs-pair-rep-02-rerun3-final-20260825T182350Z",
        "qemu-thesis-3dgs-pair-rep-03-rerun-final-20260825T195325Z",
    )
    return [root / name for name in names]


def figure_09(roots: list[Path], output: Path, summary: dict) -> None:
    values = {"classic": [], "l4s": []}; counts = {"classic": [], "l4s": []}
    for root in roots:
        data = json.loads((root / "delivery-latency-pair-support.json").read_text())
        for mode in values:
            values[mode].extend(data[mode]["latency_ms"])
            counts[mode].append({"received": data[mode]["received_objects"],
                                 "assigned": data[mode]["assigned_objects"]})
    arrays = {key: np.array(value) for key, value in values.items()}
    completed_denominator = max(len(array) for array in arrays.values())
    fig, ax = plt.subplots(figsize=(7.0, 4.3))
    for mode,color,label in (("classic",CLASSIC,"Classic: Reno / Not-ECT"),
                             ("l4s",L4S,"L4S: Prague / ECT(1)")):
        x,y=completion_cdf(arrays[mode], completed_denominator)
        ax.plot(x/1000,y,color=color,lw=2.2,label=fa(label))
        mark_percentiles(ax, arrays[mode]/1000, color, upper=mode=="l4s",
                         denominator=completed_denominator)
    ax.axvline(45, color="#59636e", lw=1.35, ls=":", label=fa("مهلت 45 ثانیه"))
    ax.set_xlabel(fa("تاخیر آمادگی تا تکمیل دریافت (ثانیه)"))
    ax.set_ylabel(fa("سهم تجمعی تکمیل‌شده‌ها؛ نرمال‌شده با بیشترین تحویل"))
    ax.set_ylim(0,1.01)
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(.5, -0.19), ncol=3,
              fontsize=8); fig.subplots_adjust(bottom=.25); style_axis(ax)
    save_figure(fig, output, "figure-09-3dgs-delivery-latency-cdf-fa")
    summary["figure_09"]={mode:{"completed_samples":len(array),"percentiles_ms":percentile_summary(array),"deadline_census":counts[mode]} for mode,array in arrays.items()}
    rows=[{"mode":mode,"percentile":pct,"delivery_latency_ms":np.percentile(array,pct)} for mode,array in arrays.items() for pct in PCTS]
    write_csv(output/"figure-09-summary.csv",list(rows[0]),rows)


def read_quality(root: Path, mode: str) -> tuple[np.ndarray,np.ndarray]:
    path=next(root.glob(f"{mode}/l4s-*-rep-*/render-lag-0ms/frame-quality.csv"))
    with path.open(newline="") as stream: rows=list(csv.DictReader(stream))
    return np.array([float(r["trace_timestamp_ms"])-3317 for r in rows]),np.array([float(r["ssim"]) for r in rows])


def figure_10(roots: list[Path], output: Path, summary: dict) -> None:
    matrices={}; time=None
    for mode in ("classic","l4s"):
        series=[]
        for root in roots:
            t,s=read_quality(root,mode); time=t if time is None else time; series.append(s)
        matrices[mode]=np.vstack(series)
    post=time>=0; post_time=time[post]/1000
    crossovers=[json.loads((root/"quality-pair-support.json").read_text())["cumulative_quality_crossover_ms"] for root in roots]
    marker=max(value for value in crossovers if value is not None)/1000
    fig,ax=plt.subplots(figsize=(7.2,4.4))
    ax.axvspan(0,marker,color="#e9edf2",alpha=.8,zorder=0)
    for mode,color,label in (("classic",CLASSIC,"Classic: Reno / Not-ECT"),
                             ("l4s",L4S,"L4S: Prague / ECT(1)")):
        matrix=matrices[mode][:,post]; mean=matrix.mean(axis=0)
        ax.plot(post_time,mean,color=color,lw=2.2,label=fa(label)); ax.fill_between(post_time,matrix.min(axis=0),matrix.max(axis=0),color=color,alpha=.16,linewidth=0)
    ax.axvline(marker,color="#59636e",lw=1.4,ls=":")
    ax.annotate(fa("پایان دوره گذار پراگ")+f"  ({marker:.3f} s)",xy=(marker,.18),xytext=(6,0),textcoords="offset points",rotation=90,va="bottom",fontsize=9,color="#59636e")
    ax.set_xlabel(fa("زمان از لحظهٔ تغییر ناگهانی دیدگاه (ثانیه)")); ax.set_ylabel("SSIM")
    ax.set_xlim(0,post_time.max()); ax.set_ylim(0,1.02); ax.legend(frameon=False,loc="lower right"); style_axis(ax)
    save_figure(fig,output,"figure-10-3dgs-quality-recovery-fa")
    rows=[]
    for index,t in enumerate(post_time):
        rows.append({"time_after_wrap_s":t,"classic_mean_ssim":matrices["classic"][:,post][:,index].mean(),"classic_min_ssim":matrices["classic"][:,post][:,index].min(),"classic_max_ssim":matrices["classic"][:,post][:,index].max(),"l4s_mean_ssim":matrices["l4s"][:,post][:,index].mean(),"l4s_min_ssim":matrices["l4s"][:,post][:,index].min(),"l4s_max_ssim":matrices["l4s"][:,post][:,index].max()})
    write_csv(output/"figure-10-summary.csv",list(rows[0]),rows)
    summary["figure_10"]={"post_wrap_mean_ssim":{mode:float(matrices[mode][:,post].mean()) for mode in matrices},"per_repetition_cumulative_quality_crossover_ms":crossovers,"plotted_conservative_transition_marker_ms":marker*1000,"transition_marker_definition":"maximum across repetitions of the earliest post-wrap time after which cumulative L4S-minus-Classic SSIM area never becomes negative"}


def write_captions(output: Path) -> None:
    text="""# زیرنویس‌های پیشنهادی

**توپولوژی شکل‌های ۶ تا ۸.** این توپولوژی در شکل‌های ۶، ۷ و ۸ استفاده شده است. میزبان‌های server و client، سوئیچ s1، جهت جریان MoQ و جریان‌های Cubic، کنترل ازدحام و گلوگاه مشترک ۲۰ مگابیت‌برثانیه نشان داده شده‌اند.

**شکل ۶.** توزیع تجمعی تجربی تاخیر مستقیم صف در گلوگاه DualPI2 برای یک جریان پس‌زمینه Cubic. نقاط p50، p95، p99 و p99.9 از تجمیع سه تکرار نشان داده شده‌اند.

**شکل ۷.** همزیستی Prague و Cubic در گلوگاه ۲۰ مگابیت‌برثانیه. میله‌ها و منحنی‌ها میانگین سه تکرار و ناحیه‌های رنگی دامنه کمینه تا بیشینه را نشان می‌دهند.

**شکل ۸.** مصالحه گذردهی و تاخیر با صفر، یک، دو و چهار جریان Cubic. نرخ پیشنهادی Cubic نامحدود است، اما همه جریان‌ها از گلوگاه مشترک ۲۰ مگابیت‌برثانیه عبور می‌کنند.

**توپولوژی شکل‌های ۹ و ۱۰.** این توپولوژی در شکل‌های ۹ و ۱۰ استفاده شده است. مسیر دوسوئیچ 3DGS، دو جریان پس‌زمینه Cubic، کنترل ازدحام، نرخ‌های فعال ۳۰۰ و ۱۰۰ مگابیت‌برثانیه و RTT برابر ۲۰ میلی‌ثانیه نشان داده شده‌اند.

**شکل ۹.** توزیع تجمعی تاخیر قابل مشاهده در کاربرد، از زمان واجدشرایط‌شدن شیء سه‌بعدی تا تکمیل دریافت آن، در افق ثابت ۴۵ ثانیه‌ای. اشیای نرسیده در وضعیت رندر باقی می‌مانند و وارد ECDF تکمیل‌ها نشده‌اند.

**شکل ۱۰.** بازیابی کیفیت دیدگاه پس از پرش دوربین در ردپای حلقوی ۵۰ درجه. خط‌چین پایان محافظه‌کارانه دوره گذار Prague را بر اساس آخرین نقطه گذار تجمعی کیفیت در سه تکرار نشان می‌دهد؛ افت کوتاه آغازین حذف نشده است.
"""
    (output/"captions-fa.md").write_text(text,encoding="utf-8")


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-root",type=Path,required=True); parser.add_argument("--l4s-root",type=Path,required=True)
    parser.add_argument("--font",type=Path,required=True); parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--write-captions", action="store_true",
                        help="write captions-fa.md; disabled for plot-only regeneration")
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True); configure(args.font)
    summary={"repetitions":3,"persian_font":str(args.font),"figure_titles":"omitted"}
    cases=network_cases(args.network_root); roots=pair_roots(args.l4s_root)
    fig, axis = plt.subplots(figsize=(8.5, 2.4)); draw_coexistence_topology(axis, fa)
    save_figure(fig, args.output, "topology-for-figures-06-08-fa")
    fig, axis = plt.subplots(figsize=(9.0, 2.7)); draw_pair_topology(axis, fa)
    save_figure(fig, args.output, "topology-for-figures-09-10-fa")
    figure_06(args.network_root,args.output,summary); figure_07(args.network_root,args.output,cases,summary); figure_08(args.output,cases,summary)
    figure_09(roots,args.output,summary); figure_10(roots,args.output,summary)
    if args.write_captions:
        write_captions(args.output)
    (args.output/"numerical-summary.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    trace=args.l4s_root/"3dgs-preparation/bicycle-start7p8-wrap7-fov50.json"; scene=args.l4s_root/"3dgs-preparation/scene.bundle"
    checksums={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in (trace,scene)}
    (args.output/"trace-scene-checksums.json").write_text(json.dumps(checksums,indent=2)+"\n")


if __name__=="__main__": main()
