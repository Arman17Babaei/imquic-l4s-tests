#!/usr/bin/env python3
"""Extract reproducible high-gain Classic-to-L4S SSIM frame examples."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def read_quality(path: Path) -> dict[int, dict[str, float]]:
    with path.open(newline="") as handle:
        return {
            int(row["frame_index"]): {
                "trace_timestamp_ms": float(row["trace_timestamp_ms"]),
                "ssim": float(row["ssim"]),
            }
            for row in csv.DictReader(handle)
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--l4s-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-post-wrap-ms", type=float, default=3317.0)
    parser.add_argument("--count", type=int, default=6)
    args = parser.parse_args()

    reps = [
        args.l4s_root / "qemu-thesis-3dgs-pair-rep-01-final-20260825T164914Z",
        args.l4s_root / "qemu-thesis-3dgs-pair-rep-02-rerun3-final-20260825T182350Z",
        args.l4s_root / "qemu-thesis-3dgs-pair-rep-03-rerun-final-20260825T195325Z",
    ]
    qualities = []
    for root in reps:
        classic = read_quality(root / "classic/l4s-0-rep-01/render-lag-0ms/frame-quality.csv")
        l4s = read_quality(root / "l4s/l4s-1-rep-01/render-lag-0ms/frame-quality.csv")
        qualities.append((classic, l4s))

    candidates = []
    common = set(qualities[0][0])
    for classic, l4s in qualities:
        common &= set(classic) & set(l4s)
    for frame in common:
        ts = qualities[0][0][frame]["trace_timestamp_ms"]
        if ts < args.min_post_wrap_ms:
            continue
        deltas = [l4s[frame]["ssim"] - classic[frame]["ssim"] for classic, l4s in qualities]
        candidates.append({
            "frame_index": frame,
            "trace_timestamp_ms": ts,
            "mean_delta_ssim": sum(deltas) / len(deltas),
            "per_repetition": [
                {
                    "classic_ssim": classic[frame]["ssim"],
                    "l4s_ssim": l4s[frame]["ssim"],
                    "delta_ssim": delta,
                }
                for (classic, l4s), delta in zip(qualities, deltas)
            ],
        })
    candidates.sort(key=lambda item: item["mean_delta_ssim"], reverse=True)

    selected = []
    for item in candidates:
        if all(abs(item["frame_index"] - other["frame_index"]) >= 20 for other in selected):
            selected.append(item)
        if len(selected) == args.count:
            break
    if len(selected) < args.count:
        raise SystemExit(f"only selected {len(selected)} of {args.count} frames")

    args.output.mkdir(parents=True, exist_ok=True)
    rep3 = reps[2]
    for item in selected:
        frame = item["frame_index"]
        for label, source in (
            ("classic", rep3 / f"classic/l4s-0-rep-01/render-lag-0ms/frames/png/frame_{frame:04d}.png"),
            ("l4s", rep3 / f"l4s/l4s-1-rep-01/render-lag-0ms/frames/png/frame_{frame:04d}.png"),
        ):
            if not source.exists():
                raise SystemExit(f"missing rendered frame: {source}")
            ssim = item["per_repetition"][2]["classic_ssim" if label == "classic" else "l4s_ssim"]
            shutil.copy2(source, args.output / f"frame-{frame:04d}-{label}-ssim-{ssim:.6f}-rep-03.png")

    (args.output / "ssim-examples.json").write_text(json.dumps({
        "selection_rule": "post-wrap frames, ranked by mean L4S-minus-Classic SSIM across three repetitions, greedy minimum spacing of 20 frames",
        "representative_images": "repetition 3",
        "min_post_wrap_ms": args.min_post_wrap_ms,
        "frames": selected,
    }, indent=2) + "\n")

    thumb_w, thumb_h = 480, 270
    sheet = Image.new("RGB", (thumb_w * 2, (thumb_h + 42) * len(selected)), "white")
    draw = ImageDraw.Draw(sheet)
    for row, item in enumerate(selected):
        y = row * (thumb_h + 42)
        for col, label in enumerate(("Classic", "L4S")):
            mode = label.lower()
            ssim_key = "classic_ssim" if mode == "classic" else "l4s_ssim"
            ssim = item["per_repetition"][2][ssim_key]
            path = args.output / f"frame-{item['frame_index']:04d}-{mode}-ssim-{ssim:.6f}-rep-03.png"
            with Image.open(path) as image:
                image.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS).save(args.output / f".tmp-{row}-{col}.jpg", quality=92)
                thumb = Image.open(args.output / f".tmp-{row}-{col}.jpg").convert("RGB")
            sheet.paste(thumb, (col * thumb_w, y))
            draw.text((col * thumb_w + 8, y + 8), label, fill="white", stroke_width=2, stroke_fill="black")
        draw.text((8, y + thumb_h + 8), f"frame {item['frame_index']} | t={item['trace_timestamp_ms']:.0f} ms | ΔSSIM={item['mean_delta_ssim']:.3f}", fill="black")
    for path in args.output.glob(".tmp-*.jpg"):
        path.unlink()
    sheet.save(args.output / "ssim-improvement-examples-contact-sheet.png")


if __name__ == "__main__":
    main()
