#!/usr/bin/env python3
"""Render partial-L4S results along the bicycle trace and compute SSIM."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import sys
import time
import types
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image

from three_dgs_bundle import _activate_dependency, image_psnr, read_bundle, require_dependency


def activate_render_dependency(dependency: Path) -> None:
    """Activate render-only client modules without loading the MoQ runtime."""
    _activate_dependency(dependency)
    package_name = "streaming.transport.client"
    if package_name in sys.modules:
        return
    package = types.ModuleType(package_name)
    package.__package__ = package_name
    package.__path__ = [
        str(Path(dependency) / "src/streaming/transport/client")
    ]
    sys.modules[package_name] = package


def gif_frame_durations_ms(
    frames: list[dict], frame_indices: list[int], fixed_duration_ms: int | None
) -> list[int]:
    if fixed_duration_ms is not None:
        return [fixed_duration_ms] * len(frame_indices)
    if not frame_indices:
        return []
    timestamps = [float(frames[index]["timestamp_ms"]) for index in frame_indices]
    differences = [
        max(1, int(round(later - earlier)))
        for earlier, later in zip(timestamps, timestamps[1:])
    ]
    final_duration = (
        max(1, int(round(statistics.median(differences))))
        if differences else 40
    )
    return [*differences, final_duration]


def quantize_gif_durations_ms(durations_ms: list[int]) -> list[int]:
    """Quantize timings to GIF's 10 ms clock without cumulative drift."""
    quantized: list[int] = []
    source_total = 0
    encoded_total = 0
    for duration_ms in durations_ms:
        source_total += duration_ms
        target_total = max(10, int(round(source_total / 10.0)) * 10)
        encoded_duration = max(10, target_total - encoded_total)
        quantized.append(encoded_duration)
        encoded_total += encoded_duration
    return quantized


def wait_for_png(path: Path, *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                with Image.open(path) as image:
                    image.verify()
                return
            except (OSError, SyntaxError):
                pass
        time.sleep(0.01)
    raise RuntimeError(f"renderer did not produce {path}")


def configure_render_logging() -> None:
    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
    )


class BundleCursor:
    """Sequential access to a received bundle using its arrival-record index."""

    def __init__(self, path: Path) -> None:
        self._records: Iterator[bytes] = iter(read_bundle(path))
        self._next_index = 0

    def pop(self, expected_index: int) -> bytes:
        if expected_index != self._next_index:
            raise RuntimeError(
                f"bundle timeline is not sequential: expected {self._next_index}, "
                f"got {expected_index}"
            )
        try:
            payload = next(self._records)
        except StopIteration as error:
            raise RuntimeError("arrival timeline references past end of bundle") from error
        self._next_index += 1
        return payload


def read_combined_timeline(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            rows.append(
                {
                    "absolute_epoch_us": int(row["absolute_epoch_us"]),
                    "path": row["path"],
                    "bundle_record_index": int(row["bundle_record_index"]),
                    "payload_bytes": int(row["payload_bytes"]),
                    "num_gaussians": int(row["num_gaussians"]),
                }
            )
    rows.sort(key=lambda row: int(row["absolute_epoch_us"]))
    return rows


def _gaussian_kernel(torch, *, size: int = 11, sigma: float = 1.5):
    coords = torch.arange(size, dtype=torch.float32) - size // 2
    kernel_1d = torch.exp(-(coords ** 2) / (2 * sigma * sigma))
    kernel_1d /= kernel_1d.sum()
    return kernel_1d[:, None] * kernel_1d[None, :]


def image_ssim(reference: Path, candidate: Path, *, device: str = "cpu") -> float:
    """Compute RGB SSIM with an 11x11, sigma=1.5 Gaussian window."""
    import torch
    import torch.nn.functional as F

    ref_np = np.asarray(Image.open(reference).convert("RGB"), dtype=np.float32) / 255.0
    got_np = np.asarray(Image.open(candidate).convert("RGB"), dtype=np.float32) / 255.0
    if ref_np.shape != got_np.shape:
        raise ValueError(f"image shape mismatch: {ref_np.shape} != {got_np.shape}")

    ref = torch.from_numpy(ref_np).permute(2, 0, 1).unsqueeze(0).to(device)
    got = torch.from_numpy(got_np).permute(2, 0, 1).unsqueeze(0).to(device)
    kernel = _gaussian_kernel(torch).to(device)
    kernel = kernel.expand(3, 1, kernel.shape[0], kernel.shape[1])

    mu_ref = F.conv2d(ref, kernel, padding=5, groups=3)
    mu_got = F.conv2d(got, kernel, padding=5, groups=3)
    mu_ref_sq = mu_ref * mu_ref
    mu_got_sq = mu_got * mu_got
    mu_cross = mu_ref * mu_got
    sigma_ref_sq = F.conv2d(ref * ref, kernel, padding=5, groups=3) - mu_ref_sq
    sigma_got_sq = F.conv2d(got * got, kernel, padding=5, groups=3) - mu_got_sq
    sigma_cross = F.conv2d(ref * got, kernel, padding=5, groups=3) - mu_cross

    c1 = 0.01 ** 2
    c2 = 0.03 ** 2
    score = (
        (2 * mu_cross + c1) * (2 * sigma_cross + c2)
        / ((mu_ref_sq + mu_got_sq + c1) * (sigma_ref_sq + sigma_got_sq + c2))
    )
    return float(score.mean().item())


def _load_manifest(cache_path: Path):
    import torch

    data = torch.load(cache_path, weights_only=False, map_location="cpu")
    manifest = data.get("manifest")
    if manifest is None:
        raise RuntimeError(f"{cache_path}: cache does not contain manifest")
    return manifest


def _full_reference_cache(source_bundle: Path):
    from streaming.transport.client.cache import SplatCache
    from streaming.transport.protocol import decode_cluster

    cache = SplatCache()
    for payload in read_bundle(source_bundle):
        cache.put(decode_cluster(payload))
    return cache


def render_references(
    source_bundle: Path,
    cache_path: Path,
    trace_path: Path,
    output: Path,
    *,
    device: str,
    width: int,
    height: int,
    frame_step: int,
    gaussian_budget: int,
    max_gaussians_per_pass: int | None,
) -> tuple[dict[int, Path], list[dict]]:
    from streaming.transport.client.pipeline import RenderPipeline
    from streaming.transport.client.viewport.trace import load_trace

    frames = load_trace(trace_path)
    pipeline = RenderPipeline(
        cache=_full_reference_cache(source_bundle),
        device=device,
        manifest=_load_manifest(cache_path),
        output_dir=output,
        image_width=width,
        image_height=height,
        render_enabled=True,
        max_gaussians_per_pass=max_gaussians_per_pass,
    )
    paths: dict[int, Path] = {}
    for frame_index in range(0, len(frames), frame_step):
        frame = frames[frame_index]
        # Offline quality must not depend on the live client's adaptive
        # performance budget or on the speed of the rendering GPU.
        pipeline._gaussian_budget = gaussian_budget
        pipeline.render_frame(
            frame_idx=frame_index,
            view_matrix=frame["view_matrix"],
            fov=frame["fov"],
            camera_position=frame["camera_position"],
            bytes_received=0,
            clusters_received=0,
        )
        png = output / "frames" / "png" / f"frame_{frame_index:04d}.png"
        wait_for_png(png)
        paths[frame_index] = png
    pipeline.close()
    return paths, frames


def render_case(
    case: Path,
    *,
    cache_path: Path,
    reference_paths: dict[int, Path],
    frames: list[dict],
    frozen_demand: dict[str, object],
    device: str,
    ssim_device: str,
    width: int,
    height: int,
    frame_step: int,
    gaussian_budget: int,
    max_gaussians_per_pass: int | None,
    evaluation_lag_ms: float,
    gif_duration_ms: int | None,
) -> dict[str, object]:
    from streaming.transport.client.cache import SplatCache
    from streaming.transport.client.pipeline import RenderPipeline
    from streaming.transport.protocol import decode_cluster

    case_result = json.loads((case / "result.json").read_text(encoding="utf-8"))
    publisher_starts = case_result["publisher_start_epoch_us"]
    anchor_path = (
        "high-prague" if "high-prague" in publisher_starts else "low-reno"
    )
    publisher_epoch_us = int(publisher_starts[anchor_path])
    initial_ms = float(frozen_demand["initial_base_release_ms"])
    time_scale = float(frozen_demand["demand_time_scale"])

    timeline = read_combined_timeline(case / "combined-arrival-timeline.csv")
    timeline_paths = sorted({str(row["path"]) for row in timeline})
    cursors = {
        name: BundleCursor(case / name / "received.bundle")
        for name in timeline_paths
    }
    candidate_cache = SplatCache()
    output = case / f"render-lag-{evaluation_lag_ms:g}ms"
    pipeline = RenderPipeline(
        cache=candidate_cache,
        device=device,
        manifest=_load_manifest(cache_path),
        output_dir=output,
        image_width=width,
        image_height=height,
        render_enabled=True,
        max_gaussians_per_pass=max_gaussians_per_pass,
    )

    timeline_index = 0
    received_objects = 0
    received_bytes = 0
    received_gaussians = 0
    rows: list[dict[str, object]] = []
    rendered_paths: list[Path] = []
    rendered_frame_indices: list[int] = []

    for frame_index in range(0, len(frames), frame_step):
        frame = frames[frame_index]
        trace_ms = float(frame["timestamp_ms"])
        logical_ms = initial_ms + trace_ms * time_scale + evaluation_lag_ms
        cutoff_epoch_us = publisher_epoch_us + int(round(logical_ms * 1000))

        while (
            timeline_index < len(timeline)
            and int(timeline[timeline_index]["absolute_epoch_us"]) <= cutoff_epoch_us
        ):
            arrival = timeline[timeline_index]
            path_name = str(arrival["path"])
            payload = cursors[path_name].pop(int(arrival["bundle_record_index"]))
            candidate_cache.put(decode_cluster(payload))
            received_objects += 1
            received_bytes += len(payload)
            received_gaussians += int(arrival["num_gaussians"])
            timeline_index += 1

        pipeline._gaussian_budget = gaussian_budget
        rendered = pipeline.render_frame(
            frame_idx=frame_index,
            view_matrix=frame["view_matrix"],
            fov=frame["fov"],
            camera_position=frame["camera_position"],
            bytes_received=received_bytes,
            clusters_received=received_objects,
        )
        candidate_png = output / "frames" / "png" / f"frame_{frame_index:04d}.png"
        if rendered is None:
            candidate_png.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (width, height), color="black").save(candidate_png)
        else:
            wait_for_png(candidate_png)
        rendered_paths.append(candidate_png)
        rendered_frame_indices.append(frame_index)
        reference_png = reference_paths[frame_index]
        rows.append(
            {
                "frame_index": frame_index,
                "trace_timestamp_ms": trace_ms,
                "evaluation_time_ms": logical_ms,
                "cutoff_epoch_us": cutoff_epoch_us,
                "received_objects": received_objects,
                "received_payload_bytes": received_bytes,
                "received_gaussians": received_gaussians,
                "ssim": image_ssim(reference_png, candidate_png, device=ssim_device),
                "psnr_db": image_psnr(reference_png, candidate_png),
            }
        )

    pipeline.close()
    metrics_path = output / "frame-quality.csv"
    fields = list(rows[0]) if rows else []
    with metrics_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    gif_path = output / "trace.gif"
    gif_durations = gif_frame_durations_ms(
        frames, rendered_frame_indices, gif_duration_ms
    )
    encoded_gif_durations = quantize_gif_durations_ms(gif_durations)
    if rendered_paths:
        images = [Image.open(path).convert("RGB") for path in rendered_paths]
        images[0].save(
            gif_path,
            save_all=True,
            append_images=images[1:],
            duration=encoded_gif_durations,
            loop=0,
        )
        for image in images:
            image.close()

    ssims = [float(row["ssim"]) for row in rows]
    psnrs = [float(row["psnr_db"]) for row in rows if row["psnr_db"] is not None]
    result = {
        "case": case.name,
        "evaluation_lag_ms": evaluation_lag_ms,
        "frames": len(rows),
        "mean_ssim": float(np.mean(ssims)) if ssims else None,
        "p05_ssim": float(np.percentile(ssims, 5)) if ssims else None,
        "mean_psnr_db": float(np.mean(psnrs)) if psnrs else None,
        "frame_quality_csv": str(metrics_path),
        "gif": str(gif_path) if gif_path.is_file() else None,
        "gif_timing": "fixed" if gif_duration_ms is not None else "trace timestamps",
        "gif_source_duration_ms": sum(gif_durations),
        "gif_duration_ms": sum(encoded_gif_durations),
        "gif_timebase_ms": 10,
        "gaussian_budget": gaussian_budget,
        "max_gaussians_per_pass": max_gaussians_per_pass,
        "compositing": (
            "track-batched-expected-depth"
            if max_gaussians_per_pass is not None else "single-pass"
        ),
    }
    (output / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--ssim-device", default="cpu")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--frame-step", type=int, default=1)
    parser.add_argument(
        "--gaussian-budget", type=int, default=5_000_000,
        help="fixed offline per-frame budget (default exceeds this scene)",
    )
    parser.add_argument(
        "--max-gaussians-per-pass",
        type=int,
        help=(
            "retain the full frame selection but rasterize complete tracks in "
            "bounded passes and composite them by expected depth"
        ),
    )
    parser.add_argument("--evaluation-lag-ms", type=float, default=0.0)
    parser.add_argument(
        "--gif-duration-ms", type=int,
        help="fixed frame duration; by default preserve sampled trace timestamps",
    )
    parser.add_argument("--allow-unpinned-3dgs", action="store_true")
    args = parser.parse_args()
    configure_render_logging()

    if args.frame_step <= 0:
        parser.error("--frame-step must be positive")
    if args.gaussian_budget <= 0:
        parser.error("--gaussian-budget must be positive")
    if args.max_gaussians_per_pass is not None and args.max_gaussians_per_pass <= 0:
        parser.error("--max-gaussians-per-pass must be positive")
    if args.evaluation_lag_ms < 0:
        parser.error("--evaluation-lag-ms must be non-negative")
    if args.gif_duration_ms is not None and args.gif_duration_ms <= 0:
        parser.error("--gif-duration-ms must be positive")
    for path in (args.root, args.source_bundle, args.cache, args.trace, args.three_dgs_dir):
        if not path.exists():
            raise SystemExit(f"missing required path: {path}")

    require_dependency(args.three_dgs_dir, allow_unpinned=args.allow_unpinned_3dgs)
    activate_render_dependency(args.three_dgs_dir)
    frozen_demand = json.loads(
        (args.root / "inputs" / "frozen-demand-order.json").read_text(encoding="utf-8")
    )
    reference_paths, frames = render_references(
        args.source_bundle,
        args.cache,
        args.trace,
        args.root / "reference-trace",
        device=args.device,
        width=args.width,
        height=args.height,
        frame_step=args.frame_step,
        gaussian_budget=args.gaussian_budget,
        max_gaussians_per_pass=args.max_gaussians_per_pass,
    )

    cases = sorted(
        path for path in args.root.iterdir()
        if path.is_dir() and path.name.startswith("l4s-")
    )
    results = [
        render_case(
            case,
            cache_path=args.cache,
            reference_paths=reference_paths,
            frames=frames,
            frozen_demand=frozen_demand,
            device=args.device,
            ssim_device=args.ssim_device,
            width=args.width,
            height=args.height,
            frame_step=args.frame_step,
            gaussian_budget=args.gaussian_budget,
            max_gaussians_per_pass=args.max_gaussians_per_pass,
            evaluation_lag_ms=args.evaluation_lag_ms,
            gif_duration_ms=args.gif_duration_ms,
        )
        for case in cases
    ]
    (args.root / f"quality-lag-{args.evaluation_lag_ms:g}ms.json").write_text(
        json.dumps(
            {
                "scenario": "3dgs-partial-l4s-post-send-reordering",
                "evaluation_lag_ms": args.evaluation_lag_ms,
                "frame_step": args.frame_step,
                "gaussian_budget": args.gaussian_budget,
                "max_gaussians_per_pass": args.max_gaussians_per_pass,
                "compositing": (
                    "track-batched-expected-depth"
                    if args.max_gaussians_per_pass is not None else "single-pass"
                ),
                "cases": results,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
