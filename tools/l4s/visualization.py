#!/usr/bin/env python3
"""Standard visualization conventions for L4S experiment timelines.

The module intentionally keeps experiment parsing out of plotting.  Callers pass
normalized rows/summary records, so future Prague/DualPI2/MoQ analyzers can reuse
the same visual grammar.
"""

from __future__ import annotations

from pathlib import Path
import math
from typing import Iterable, Sequence

FLOW_STYLES = {
    "A": {"color": "#0072B2", "linestyle": "-", "label": "Flow A"},
    "B": {"color": "#D55E00", "linestyle": "--", "label": "Flow B"},
}
PHASE_SHADE = ("#f8fafc", "#eef2f7")


def _pyplot():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError(
            "standard plots require matplotlib (for example: apt install python3-matplotlib)"
        ) from error
    return plt


def _save(fig, stem: Path) -> None:
    stem.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(stem.with_suffix(".svg"), bbox_inches="tight")
    fig.savefig(stem.with_suffix(".png"), dpi=180, bbox_inches="tight")


def _phase_background(ax, boundaries: Sequence[float], labels: Sequence[str],
                      annotate: bool = False) -> None:
    if len(boundaries) != len(labels) + 1:
        raise ValueError("phase boundaries/labels mismatch")
    for index, label in enumerate(labels):
        left, right = boundaries[index], boundaries[index + 1]
        ax.axvspan(left, right, color=PHASE_SHADE[index % 2], zorder=-10)
        if annotate:
            ax.text(
                (left + right) / 2,
                0.98,
                label,
                transform=ax.get_xaxis_transform(),
                ha="center",
                va="top",
                fontsize=8,
            )


def _event_lines(axes, events: Sequence[dict], debug: bool) -> None:
    labels = {
        ("B1", "start"): "B starts",
        ("A1", "end"): "A leaves",
        ("A2", "start"): "A returns",
        ("B1", "end"): "B leaves",
    }
    for event in events:
        key = (event["instance"], event["event"])
        if key not in labels:
            continue
        x = float(event["time_s"])
        for ax in axes:
            ax.axvline(x, color="#64748b", linewidth=0.8, alpha=0.7)
        if debug:
            axes[0].text(
                x,
                1.02,
                labels[key],
                transform=axes[0].get_xaxis_transform(),
                rotation=90,
                va="bottom",
                ha="center",
                fontsize=8,
            )


def _flow_rows(rows: Iterable[dict], flow: str) -> list[dict]:
    return sorted(
        (row for row in rows if row["flow"] == flow),
        key=lambda row: float(row["time_s"]),
    )


def plot_fairness_timeline(rows: Sequence[dict], events: Sequence[dict],
                           boundaries: Sequence[float], labels: Sequence[str],
                           bottleneck_mbps: float, output_stem: Path,
                           plot_mode: str = "debug", run_label: str = "") -> None:
    """Render the canonical throughput/share/cwnd/RTT explanatory figure."""
    plt = _pyplot()
    debug = plot_mode == "debug"
    if plot_mode not in ("debug", "paper"):
        raise ValueError("plot mode must be debug or paper")
    figure, axes = plt.subplots(4, 1, figsize=(11.5, 9.2), sharex=True)
    ax_rate, ax_share, ax_cwnd, ax_rtt = axes

    for ax in axes:
        _phase_background(ax, boundaries, labels, annotate=ax is ax_rate)
        ax.grid(axis="y", alpha=0.22)

    for flow in ("A", "B"):
        series = _flow_rows(rows, flow)
        style = FLOW_STYLES[flow]
        x = [float(row["time_s"]) for row in series]
        rate = [float(row["throughput_mbps"]) for row in series]
        ax_rate.plot(x, rate, linewidth=1.7, **style)
        cwnd_x = [float(row["time_s"]) for row in series if row["cwnd_bytes"] not in ("", None)]
        cwnd = [float(row["cwnd_bytes"]) / 1024 for row in series
                if row["cwnd_bytes"] not in ("", None)]
        if cwnd_x:
            ax_cwnd.plot(cwnd_x, cwnd, linewidth=1.4, **style)
        rtt_x = [float(row["time_s"]) for row in series if row["rtt_ms"] not in ("", None)]
        rtt = [float(row["rtt_ms"]) for row in series if row["rtt_ms"] not in ("", None)]
        if rtt_x:
            ax_rtt.plot(rtt_x, rtt, linewidth=1.4, **style)
        retrans_x = [float(row["time_s"]) for row in series
                     if int(float(row.get("retransmissions", 0))) > 0]
        if retrans_x:
            ax_rtt.scatter(
                retrans_x,
                [0.04] * len(retrans_x),
                transform=ax_rtt.get_xaxis_transform(),
                marker="x",
                s=24,
                color=style["color"],
                label=f"{flow} retransmission",
                zorder=5,
            )

    a_rows = _flow_rows(rows, "A")
    share_x = [float(row["time_s"]) for row in a_rows if row.get("flow_share") not in ("", None)]
    shares = [float(row["flow_share"]) for row in a_rows if row.get("flow_share") not in ("", None)]
    ax_share.plot(share_x, shares, color=FLOW_STYLES["A"]["color"], linewidth=1.6,
                  label="A share")
    ax_share.axhline(0.5, color="#111827", linewidth=1.0, linestyle=":", label="equal share")
    ax_share.set_ylim(0, 1)

    ax_rate.axhline(bottleneck_mbps, color="#111827", linewidth=1.0, linestyle=":",
                    label="bottleneck")
    ax_rate.set_ylim(bottom=0)
    ax_cwnd.set_ylim(bottom=0)
    ax_rtt.set_ylim(bottom=0)
    ax_rate.set_ylabel("Throughput\n(Mbit/s)")
    ax_share.set_ylabel("Flow A\nshare")
    ax_cwnd.set_ylabel("cwnd\n(KiB)")
    ax_rtt.set_ylabel("RTT\n(ms)")
    ax_rtt.set_xlabel("Experiment time (s)")
    ax_rate.legend(loc="upper right", ncol=3, fontsize=8)
    ax_share.legend(loc="upper right", ncol=2, fontsize=8)
    ax_rtt.legend(loc="upper right", ncol=2, fontsize=8)
    _event_lines(axes, events, debug)
    title = "Reno/Reno five-phase fairness timeline"
    if debug and run_label:
        title += f" — {run_label}"
    figure.suptitle(title)
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    _save(figure, output_stem)
    plt.close(figure)


def plot_convergence_comparison(rows: Sequence[dict], boundaries: Sequence[float],
                                output_stem: Path, plot_mode: str = "debug") -> None:
    """Align the two newcomer arrivals and compare incumbent-share convergence."""
    plt = _pyplot()
    a_rows = _flow_rows(rows, "A")
    figure, ax = plt.subplots(figsize=(8.5, 4.3))
    comparisons = (
        (boundaries[1], boundaries[2], False, "A incumbent; B joins", "-"),
        (boundaries[3], boundaries[4], True, "B incumbent; A joins", "--"),
    )
    for start, end, invert, label, linestyle in comparisons:
        points = [row for row in a_rows
                  if start <= float(row["time_s"]) < end
                  and row.get("flow_share") not in ("", None)]
        x = [float(row["time_s"]) - start for row in points]
        a_share = [float(row["flow_share"]) for row in points]
        incumbent = [1 - value if invert else value for value in a_share]
        ax.plot(x, incumbent, linewidth=1.7, linestyle=linestyle, label=label)
    ax.axhline(0.5, color="#111827", linewidth=1.0, linestyle=":", label="equal share")
    ax.axhspan(0.45, 0.55, color="#e5e7eb", alpha=0.65, zorder=-5)
    ax.set_ylim(0, 1)
    ax.set_xlim(left=0)
    ax.set_xlabel("Time since newcomer arrival (s)")
    ax.set_ylabel("Incumbent throughput share")
    ax.set_title("Mirrored Reno convergence")
    ax.grid(axis="y", alpha=0.22)
    ax.legend(fontsize=8)
    figure.tight_layout()
    _save(figure, output_stem)
    plt.close(figure)


def plot_repetition_phase_summary(summaries: Sequence[dict], output_stem: Path) -> None:
    """Show individual repetitions and means for the two mirrored overlaps."""
    plt = _pyplot()
    figure, ax = plt.subplots(figsize=(7.8, 4.6))
    categories = (
        (1, "A incumbent\n(B joins)", "phase2_incumbent_share"),
        (2, "B incumbent\n(A joins)", "phase4_incumbent_share"),
    )
    for x, _, field in categories:
        values = [float(row[field]) for row in summaries
                  if row.get(field) is not None and math.isfinite(float(row[field]))]
        offsets = [((index % 5) - 2) * 0.025 for index in range(len(values))]
        ax.scatter([x + offset for offset in offsets], values, s=30, alpha=0.8)
        if values:
            mean = sum(values) / len(values)
            ax.hlines(mean, x - 0.22, x + 0.22, color="#111827", linewidth=2.0)
    ax.axhline(0.5, color="#111827", linewidth=1.0, linestyle=":")
    ax.axhspan(0.45, 0.55, color="#e5e7eb", alpha=0.65, zorder=-5)
    ax.set_xlim(0.5, 2.5)
    ax.set_ylim(0, 1)
    ax.set_xticks([item[0] for item in categories], [item[1] for item in categories])
    ax.set_ylabel("Incumbent throughput share")
    ax.set_title("Reno/Reno overlap fairness across repetitions")
    ax.grid(axis="y", alpha=0.22)
    figure.tight_layout()
    _save(figure, output_stem)
    plt.close(figure)


def plot_solo_check(summaries: Sequence[dict], output_stem: Path,
                    bottleneck_mbps: float) -> None:
    """Compare the three solo periods to expose baseline/testbed asymmetry."""
    plt = _pyplot()
    figure, ax = plt.subplots(figsize=(7.8, 4.6))
    categories = (
        (1, "A before", "solo_A_before_mbps"),
        (2, "B alone", "solo_B_mbps"),
        (3, "A after", "solo_A_after_mbps"),
    )
    for x, _, field in categories:
        values = [float(row[field]) for row in summaries
                  if row.get(field) is not None and math.isfinite(float(row[field]))]
        offsets = [((index % 5) - 2) * 0.025 for index in range(len(values))]
        ax.scatter([x + offset for offset in offsets], values, s=30, alpha=0.8)
        if values:
            mean = sum(values) / len(values)
            ax.hlines(mean, x - 0.22, x + 0.22, color="#111827", linewidth=2.0)
    ax.axhline(bottleneck_mbps, color="#111827", linewidth=1.0, linestyle=":",
               label="configured bottleneck")
    ax.set_ylim(bottom=0)
    ax.set_xticks([item[0] for item in categories], [item[1] for item in categories])
    ax.set_ylabel("Throughput (Mbit/s)")
    ax.set_title("Solo-flow symmetry check")
    ax.grid(axis="y", alpha=0.22)
    ax.legend(fontsize=8)
    figure.tight_layout()
    _save(figure, output_stem)
    plt.close(figure)


def plot_step_join_timeline(rows: Sequence[dict], boundaries: Sequence[float],
                            labels: Sequence[str], bottleneck_mbps: float,
                            output_stem: Path, aggregate: bool = False,
                            run_label: str = "",
                            congestion_label: str = "Reno") -> None:
    """Plot ten cumulative independent streams in throughput and cwnd panels."""
    plt = _pyplot()
    figure, axes = plt.subplots(2, 1, figsize=(12, 7.4), sharex=True)
    ax_rate, ax_cwnd = axes
    for ax in axes:
        _phase_background(ax, boundaries, labels, annotate=ax is ax_rate)
        ax.grid(axis="y", alpha=0.22)
        for boundary in boundaries[1:-1]:
            ax.axvline(boundary, color="#64748b", linewidth=0.7, alpha=0.6)

    streams = sorted({str(row["stream"]) for row in rows})
    colors = plt.get_cmap("tab10").colors
    for index, stream in enumerate(streams):
        series = sorted(
            (row for row in rows
             if row["stream"] == stream
             and int(row.get("active", row.get("active_repetitions", 0))) > 0),
            key=lambda row: float(row["time_s"]),
        )
        color = colors[index % len(colors)]
        if aggregate:
            rate_field, cwnd_field = "throughput_mbps_mean", "cwnd_bytes_mean"
        else:
            rate_field, cwnd_field = "throughput_mbps", "cwnd_bytes"
        rate_rows = [row for row in series if row.get(rate_field) not in (None, "")]
        rate_x = [float(row["time_s"]) for row in rate_rows]
        rate_y = [float(row[rate_field]) for row in rate_rows]
        ax_rate.plot(rate_x, rate_y, color=color, linewidth=1.35, label=stream)
        cwnd_rows = [row for row in series if row.get(cwnd_field) not in (None, "")]
        cwnd_x = [float(row["time_s"]) for row in cwnd_rows]
        cwnd_y = [float(row[cwnd_field]) / 1024 for row in cwnd_rows]
        ax_cwnd.plot(cwnd_x, cwnd_y, color=color, linewidth=1.25, label=stream)
        if aggregate:
            rate_sd = [float(row.get("throughput_mbps_stdev") or 0) for row in rate_rows]
            ax_rate.fill_between(
                rate_x,
                [max(0.0, value - deviation) for value, deviation in zip(rate_y, rate_sd)],
                [value + deviation for value, deviation in zip(rate_y, rate_sd)],
                color=color, alpha=0.09, linewidth=0,
            )
            cwnd_sd = [float(row.get("cwnd_bytes_stdev") or 0) / 1024
                       for row in cwnd_rows]
            ax_cwnd.fill_between(
                cwnd_x,
                [max(0.0, value - deviation) for value, deviation in zip(cwnd_y, cwnd_sd)],
                [value + deviation for value, deviation in zip(cwnd_y, cwnd_sd)],
                color=color, alpha=0.09, linewidth=0,
            )

    ax_rate.axhline(bottleneck_mbps, color="#111827", linewidth=1.0,
                    linestyle=":", label="bottleneck")
    ax_rate.set_ylabel("Throughput\n(Mbit/s)")
    ax_cwnd.set_ylabel("cwnd\n(KiB)")
    ax_cwnd.set_xlabel("Experiment time (s)")
    ax_rate.set_ylim(bottom=0)
    ax_cwnd.set_ylim(bottom=0)
    ax_cwnd.set_xlim(boundaries[0], boundaries[-1])
    ax_rate.legend(loc="upper right", ncol=4, fontsize=7)
    title = f"Independent {congestion_label} streams joining cumulatively"
    if aggregate:
        title += " — repetition mean ±1 SD"
    elif run_label:
        title += f" — {run_label}"
    figure.suptitle(title)
    figure.tight_layout(rect=(0, 0, 1, 0.96))
    _save(figure, output_stem)
    plt.close(figure)
