#!/usr/bin/env python3
"""Regenerate the stored through-run 3DGS quality comparison in Persian."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
import numpy as np

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ModuleNotFoundError as error:
    raise SystemExit(
        "Persian shaping dependencies are required: pip install arabic-reshaper python-bidi"
    ) from error


INK = "#28323c"
GRID = "#d8dde3"
CASES = (
    ("l4s-classic-100-rep-01", "classic", 1.0),
    ("l4s-l4s-100-rep-01", "dualpi2", 1.0),
    ("l4s-classic-25-rep-01", "classic", 0.25),
    ("l4s-l4s-25-rep-01", "dualpi2", 0.25),
)
COLORS = {"classic": "#c34a44", "dualpi2": "#266fa5"}
STYLES = {1.0: "-", 0.25: (0, (5, 2))}


def fa(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def configure(font: Path) -> None:
    font_manager.fontManager.addfont(str(font))
    name = font_manager.FontProperties(fname=str(font)).get_name()
    matplotlib.rcParams.update({
        "font.family": name,
        "font.size": 10.5,
        "axes.edgecolor": INK,
        "axes.labelcolor": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "text.color": INK,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def provenance_for(case: Path) -> tuple[Path, dict]:
    resolved = case.resolve()
    provenance = resolved.parent / "provenance.json"
    if not provenance.is_file():
        raise ValueError(f"missing provenance: {provenance}")
    return provenance, json.loads(provenance.read_text())["configuration"]


def label(mode: str, fraction: float) -> str:
    share = "Prague 100%" if fraction == 1.0 else "Prague 25%"
    bottleneck = "گلوگاه دوم کلاسیک" if mode == "classic" else "گلوگاه دوم DualPI2"
    return share + " - " + fa(bottleneck)


def threshold_time(rows: list[dict[str, str]], field: str, threshold: float) -> float | None:
    for row in rows:
        if float(row[field]) >= threshold:
            return float(row["trace_timestamp_ms"]) / 1000.0
    return None


def summarize(name: str, mode: str, fraction: float, rows: list[dict[str, str]]) -> dict:
    time_s = np.array([float(row["trace_timestamp_ms"]) / 1000.0 for row in rows])
    ssim = np.array([float(row["ssim"]) for row in rows])
    psnr = np.array([float(row["psnr_db"]) for row in rows])
    duration = time_s[-1] - time_s[0]
    return {
        "case": name,
        "downstream_mode": mode,
        "prague_fraction": fraction,
        "frames": len(rows),
        "duration_s": float(duration),
        "mean_ssim": float(np.mean(ssim)),
        "p05_ssim": float(np.percentile(ssim, 5)),
        "time_weighted_ssim": float(np.trapz(ssim, time_s) / duration),
        "final_ssim": float(ssim[-1]),
        "mean_psnr_db": float(np.mean(psnr)),
        "time_weighted_psnr_db": float(np.trapz(psnr, time_s) / duration),
        "final_psnr_db": float(psnr[-1]),
        "final_received_objects": int(rows[-1]["received_objects"]),
        "final_received_payload_bytes": int(rows[-1]["received_payload_bytes"]),
        "final_received_gaussians": int(rows[-1]["received_gaussians"]),
        "time_to_ssim_0p5_s": threshold_time(rows, "ssim", 0.5),
        "time_to_ssim_0p7_s": threshold_time(rows, "ssim", 0.7),
        "time_to_psnr_15_db_s": threshold_time(rows, "psnr_db", 15.0),
        "time_to_psnr_20_db_s": threshold_time(rows, "psnr_db", 20.0),
    }


def compatibility_gate(configurations: list[tuple[str, str, float, dict]]) -> dict:
    invariant_keys = (
        "application_admission_gate", "application_priority", "base_rtt_ms",
        "capture_start", "classic_buffer_packets", "connections",
        "dc_background_cc", "dc_background_mbps", "deadline_ms",
        "demand_time_scale", "downstream_rate", "dualpi2_step", "dualpi2_target",
        "dualpi2_tupdate", "frozen_demand_sha256", "initial_release_ms",
        "packet_order_capture", "provider_dualpi2_rate", "repetitions",
        "source_sha256", "trace_sha256", "transport_assignment",
        "transport_queue_slack_bytes",
    )
    reference = configurations[0][3]
    mismatches = []
    for name, expected_mode, fraction, config in configurations:
        for key in invariant_keys:
            if config.get(key) != reference.get(key):
                mismatches.append({"case": name, "field": key,
                                   "actual": config.get(key), "expected": reference.get(key)})
        if config.get("downstream_mode") != expected_mode:
            mismatches.append({"case": name, "field": "downstream_mode",
                               "actual": config.get("downstream_mode"),
                               "expected": expected_mode})
        if config.get("l4s_fractions") != [fraction]:
            mismatches.append({"case": name, "field": "l4s_fractions",
                               "actual": config.get("l4s_fractions"), "expected": [fraction]})
    if mismatches:
        raise ValueError(f"comparison provenance mismatch: {mismatches}")
    return {key: reference[key] for key in invariant_keys}


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def save_figure(fig: plt.Figure, output: Path, stem: str) -> None:
    for suffix in ("pdf", "svg", "png"):
        fig.savefig(output / f"{stem}.{suffix}", bbox_inches="tight",
                    dpi=240 if suffix == "png" else None)
    plt.close(fig)


def separate_figure(data: list[tuple], output: Path, *, field: str, stem: str,
                    ylabel: str, ylim: tuple[float, float] | None = None) -> None:
    fig, axis = plt.subplots(figsize=(8.4, 4.15))
    for _name, mode, fraction, rows in data:
        time_s = [float(row["trace_timestamp_ms"]) / 1000.0 for row in rows]
        axis.plot(time_s, [float(row[field]) for row in rows],
                  color=COLORS[mode], linestyle=STYLES[fraction], linewidth=1.8,
                  label=label(mode, fraction))
    axis.set_xlabel(fa("زمان مسیر دوربین (ثانیه)"))
    axis.set_ylabel(ylabel)
    axis.set_xlim(0, 11.2)
    if ylim is not None:
        axis.set_ylim(*ylim)
    axis.grid(True, color=GRID, linewidth=.65)
    axis.set_axisbelow(True)
    axis.legend(frameon=False, fontsize=8.2, ncol=2, loc="upper left")
    axis.text(.985, .055, fa("یک تکرار برای هر حالت؛ مقایسه توصیفی"),
              transform=axis.transAxes, ha="right", fontsize=8.6, color="#8b3a3a")
    save_figure(fig, output, stem)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--font", type=Path, default=Path("fonts/XB Niloofar.ttf"))
    args = parser.parse_args()
    output = args.output_root or args.comparison_root / "quality-plots"
    output.mkdir(parents=True, exist_ok=True)
    configure(args.font)

    data = []
    configs = []
    source_records = []
    reference_frames = None
    for name, mode, fraction in CASES:
        case = args.comparison_root / name
        quality = case / "render-lag-0ms/frame-quality.csv"
        if not quality.is_file():
            raise ValueError(f"missing quality evidence: {quality}")
        rows = read_csv(quality)
        frame_axis = [(int(row["frame_index"]), float(row["trace_timestamp_ms"])) for row in rows]
        if reference_frames is None:
            reference_frames = frame_axis
        elif frame_axis != reference_frames:
            raise ValueError(f"frame/timestamp mismatch: {name}")
        provenance_path, config = provenance_for(case)
        configs.append((name, mode, fraction, config))
        source_records.append({
            "case": name,
            "quality_csv": str(quality.resolve()),
            "quality_csv_sha256": sha256(quality),
            "provenance": str(provenance_path.resolve()),
            "provenance_sha256": sha256(provenance_path),
        })
        data.append((name, mode, fraction, rows))
    invariants = compatibility_gate(configs)

    fig, axes = plt.subplots(2, 1, figsize=(9.3, 6.4), sharex=True)
    for name, mode, fraction, rows in data:
        time_s = np.array([float(row["trace_timestamp_ms"]) / 1000.0 for row in rows])
        axes[0].plot(time_s, [float(row["ssim"]) for row in rows],
                     color=COLORS[mode], linestyle=STYLES[fraction], linewidth=1.7,
                     label=label(mode, fraction))
        axes[1].plot(time_s, [float(row["psnr_db"]) for row in rows],
                     color=COLORS[mode], linestyle=STYLES[fraction], linewidth=1.7)
    axes[0].set_ylabel("SSIM  " + fa("شباهت ساختاری"))
    axes[1].set_ylabel("PSNR (dB)  " + fa("کیفیت بازسازی"))
    axes[1].set_xlabel(fa("زمان مسیر دوربین (ثانیه)"))
    axes[0].set_ylim(0, 1.02)
    axes[1].set_xlim(0, 11.2)
    axes[0].legend(frameon=False, fontsize=8.2, ncol=2, loc="upper left")
    axes[1].text(.985, .055, fa("یک تکرار برای هر حالت؛ مقایسه توصیفی"),
                 transform=axes[1].transAxes, ha="right", fontsize=8.6,
                 color="#8b3a3a")
    for axis in axes:
        axis.grid(True, color=GRID, linewidth=.65)
        axis.set_axisbelow(True)
    fig.subplots_adjust(hspace=.08)
    stem = "ssim-psnr-through-run-fa"
    save_figure(fig, output, stem)
    separate_stems = ("ssim-through-run-fa", "psnr-through-run-fa")
    separate_figure(data, output, field="ssim", stem=separate_stems[0],
                    ylabel="SSIM  " + fa("شباهت ساختاری"), ylim=(0, 1.02))
    separate_figure(data, output, field="psnr_db", stem=separate_stems[1],
                    ylabel="PSNR (dB)  " + fa("کیفیت بازسازی"))

    summaries = [summarize(*item) for item in data]
    write_csv(output / f"{stem}-summary.csv", summaries)
    by_key = {(row["downstream_mode"], row["prague_fraction"]): row for row in summaries}
    deltas = {}
    for fraction in (1.0, 0.25):
        classic = by_key[("classic", fraction)]
        dualpi2 = by_key[("dualpi2", fraction)]
        deltas[str(fraction)] = {
            "dualpi2_minus_classic_mean_ssim": dualpi2["mean_ssim"] - classic["mean_ssim"],
            "dualpi2_minus_classic_mean_psnr_db": (
                dualpi2["mean_psnr_db"] - classic["mean_psnr_db"]),
            "dualpi2_minus_classic_final_ssim": dualpi2["final_ssim"] - classic["final_ssim"],
            "dualpi2_minus_classic_final_psnr_db": (
                dualpi2["final_psnr_db"] - classic["final_psnr_db"]),
        }
    summary = {
        "schema_version": 1,
        "claim_type": "single_repetition_descriptive_comparison",
        "statistical_significance_supported": False,
        "compatibility_gate": {"passed": True, "invariants": invariants},
        "cases": summaries,
        "paired_descriptive_deltas": deltas,
        "sources": source_records,
        "metric_scope": "rendered frames compared with one shared complete-scene reference trace",
    }
    summary_path = output / f"{stem}-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    caption_path = output / f"{stem}-caption-fa.md"
    caption_path.write_text(
        "**کیفیت بازسازی در طول مسیر دوربین.** هر منحنی ۵۰۰ قاب را در برابر "
        "مرجع کامل مشترک نشان می‌دهد. رنگ، نوع گلوگاه دوم و نوع خط، سهم داده "
        "ارسال‌شده با Prague را مشخص می‌کند. نرخ گلوگاه ۵۰ مگابیت‌برثانیه، RTT "
        "برابر ۲۰ میلی‌ثانیه و بار پس‌زمینه رنو ۲۸۰ مگابیت‌برثانیه است؛ تنظیمات "
        "DualPI2 برابر target=15ms، tupdate=16ms و step_thresh=5ms است. برای هر "
        "حالت تنها یک تکرار موجود است؛ بنابراین تفاوت‌ها توصیفی‌اند و فاصله اطمینان "
        "یا معناداری آماری گزارش نمی‌شود.\n",
        encoding="utf-8",
    )
    setup_path = output / f"{stem}-setup.md"
    setup_path.write_text(
        "# Experimental setup: L4S-L4S versus L4S-Classic quality trace\n\n"
        "## Scientific question\n\n"
        "With the same frozen 3DGS demand and sender scheduling policy, how does "
        "changing the second/client-facing bottleneck from Classic to DualPI2 affect "
        "rendered SSIM and PSNR over the camera trace?\n\n"
        "## Compared conditions\n\n"
        "| Prague payload share | Second bottleneck | Plot style |\n"
        "|---:|---|---|\n"
        "| 100% | Classic | Solid red |\n"
        "| 100% | DualPI2 | Solid blue |\n"
        "| 25% | Classic | Dashed red |\n"
        "| 25% | DualPI2 | Dashed blue |\n\n"
        "The remaining payload in the 25% cases is carried by Reno.\n\n"
        "## Network and congestion-control tuple\n\n"
        "- Two persistent media connections: Prague and Reno.\n"
        "- Assignment: importance-ranked payload-byte prefix to Prague; remainder to Reno.\n"
        "- Provider bottleneck: DualPI2 at 300 Mbit/s.\n"
        "- Second/client-facing bottleneck: 50 Mbit/s, varied between Classic and DualPI2.\n"
        "- Base RTT: 20 ms, split across forward data and reverse ACK paths.\n"
        "- Background flow: 280 Mbit/s Reno; it shares the provider egress but not the "
        "client-facing qdisc.\n"
        "- DualPI2: `target=15ms`, `tupdate=16ms`, `step_thresh=5ms`.\n"
        "- Classic buffer: 128 packets.\n"
        "- Sender transport queue slack: 4,096 bytes.\n"
        "- Deadline: 30,000 ms.\n"
        "- Capture mode: packet log, started after both QUIC connections were ready.\n\n"
        "## Application scheduling\n\n"
        "- Priority: progressive layer/subgroup ascending, mean opacity descending, "
        "then stable source order.\n"
        "- Admission gate: queued-stream bytes must remain below the object/slack limit, "
        "and bytes in flight must remain below the congestion window.\n"
        "- Initial release: 5 ms.\n"
        "- Frozen demand, source bundle, and camera-trace hashes are identical in all "
        "four conditions.\n\n"
        "## Rendering and quality evaluation\n\n"
        "- Workload: frozen 3DGS bicycle camera trace.\n"
        "- Evaluation: 500 frames covering 11.12 seconds.\n"
        "- Reference: one shared complete-scene reference trace.\n"
        "- Metrics: per-frame SSIM and PSNR.\n"
        "- Evaluation lag: 0 ms.\n"
        "- Compositing: track-batched expected depth.\n"
        "- Gaussian budget: 5,000,000.\n"
        "- Maximum Gaussians per render pass: 300,000 for the 100% Prague cases and "
        "400,000 for the 25% Prague cases. This does not affect Classic-versus-DualPI2 "
        "comparisons at a fixed Prague share, but it confounds direct 100%-versus-25% "
        "comparisons.\n\n"
        "## RTT and bandwidth plot definitions\n\n"
        "- RTT is read directly from each sender's `transport-metrics.csv` and shown as "
        "the median within 100 ms bins. The Prague panel includes all four conditions; "
        "the Reno panel includes the two 25% Prague conditions because Reno carries no "
        "media in the 100% cases.\n"
        "- Bandwidth is UDP payload observed at the second-bottleneck egress (`s2-eth2`) "
        "for media source ports 4443 and 4444, summed in 500 ms bins. It includes QUIC "
        "overhead and retransmissions and therefore is wire payload throughput, not "
        "application goodput.\n"
        "- Both network plots use the same 11.12-second horizon as the rendered quality "
        "trace.\n\n"
        "## Evidence strength and caveats\n\n"
        "- One repetition is available per condition. Results are descriptive; the "
        "dataset does not support confidence intervals or statistical-significance claims.\n"
        "- The provenance compatibility gate passed, and all four inputs have identical "
        "frame indices and trace timestamps.\n"
        "- Conclusions are scoped to this topology, workload, controllers, queue settings, "
        "and rendering configuration.\n\n"
        "Machine-readable values and source hashes are in "
        "[`ssim-psnr-through-run-fa-summary.json`](ssim-psnr-through-run-fa-summary.json).\n",
        encoding="utf-8",
    )
    artifacts = {}
    for figure_stem in (stem, *separate_stems):
        for suffix in ("pdf", "svg", "png"):
            path = output / f"{figure_stem}.{suffix}"
            artifacts[path.name] = sha256(path)
    for suffix in ("csv", "json"):
        path = output / f"{stem}-summary.{suffix}"
        artifacts[path.name] = sha256(path)
    artifacts[caption_path.name] = sha256(caption_path)
    artifacts[setup_path.name] = sha256(setup_path)
    (output / f"{stem}-manifest.json").write_text(json.dumps({
        "artifacts": artifacts,
        "verification": {
            "frame_axis_identical": True,
            "frames_per_case": len(reference_frames or []),
            "provenance_gate_passed": True,
            "persian_shaping": True,
            "separate_metric_pdfs": True,
        },
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
