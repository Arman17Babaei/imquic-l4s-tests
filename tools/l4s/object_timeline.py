#!/usr/bin/env python3
"""Validate per-object 3DGS arrival timelines produced by the IMQUIC subscriber."""

from __future__ import annotations

import csv
import hashlib
import html
import math
from pathlib import Path

FIELDS = (
    "arrival_time_us",
    "bundle_record_index",
    "payload_bytes",
    "cumulative_bytes",
    "num_gaussians",
    "cumulative_gaussians",
    "subgroup_id",
    "object_id",
)


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize_timeline(path: Path) -> dict[str, object]:
    """Validate ordering/cumulative counters and return compact provenance."""
    path = Path(path)
    objects = 0
    cumulative_bytes = 0
    cumulative_gaussians = 0
    first_arrival_us: int | None = None
    last_arrival_us: int | None = None

    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(
                f"{path}: unexpected arrival timeline columns {reader.fieldnames!r}"
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                values = {field: int(row[field]) for field in FIELDS}
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{path}:{line_number}: invalid integer arrival timeline field"
                ) from error

            if values["bundle_record_index"] != objects:
                raise ValueError(
                    f"{path}:{line_number}: bundle record index "
                    f"{values['bundle_record_index']} != expected {objects}"
                )
            arrival_us = values["arrival_time_us"]
            if arrival_us < 0 or (
                last_arrival_us is not None and arrival_us < last_arrival_us
            ):
                raise ValueError(f"{path}:{line_number}: arrival times are not monotonic")
            if values["payload_bytes"] <= 0 or values["num_gaussians"] <= 0:
                raise ValueError(f"{path}:{line_number}: object sizes must be positive")
            if values["subgroup_id"] not in (0, 1, 2):
                raise ValueError(f"{path}:{line_number}: invalid subgroup_id")
            if values["object_id"] < 0:
                raise ValueError(f"{path}:{line_number}: object_id must be non-negative")

            cumulative_bytes += values["payload_bytes"]
            cumulative_gaussians += values["num_gaussians"]
            if values["cumulative_bytes"] != cumulative_bytes:
                raise ValueError(
                    f"{path}:{line_number}: cumulative byte count mismatch"
                )
            if values["cumulative_gaussians"] != cumulative_gaussians:
                raise ValueError(
                    f"{path}:{line_number}: cumulative Gaussian count mismatch"
                )

            if first_arrival_us is None:
                first_arrival_us = arrival_us
            last_arrival_us = arrival_us
            objects += 1

    return {
        "objects": objects,
        "payload_bytes": cumulative_bytes,
        "gaussians": cumulative_gaussians,
        "first_arrival_us": first_arrival_us,
        "last_arrival_us": last_arrival_us,
        "sha256": sha256_file(path),
    }


def prefix_object_count(path: Path, presentation_time_us: int) -> int:
    """Return how many leading received.bundle records existed by a presentation time."""
    if presentation_time_us < 0:
        raise ValueError("presentation_time_us must be non-negative")
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(f"{path}: unexpected arrival timeline columns")
        count = 0
        for row in reader:
            if int(row["arrival_time_us"]) > presentation_time_us:
                break
            count += 1
    return count


def timeline_series(path: Path, bin_us: int = 1_000_000) -> dict[str, object]:
    """Return cumulative and fixed-width splat/payload-arrival series."""
    if bin_us <= 0:
        raise ValueError("bin_us must be positive")
    cumulative: list[tuple[int, int]] = [(0, 0)]
    bins: list[int] = []
    byte_bins: list[int] = []
    total = 0
    total_bytes = 0
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(f"{path}: unexpected arrival timeline columns")
        last_time = -1
        for line_number, row in enumerate(reader, start=2):
            try:
                arrival_us = int(row["arrival_time_us"])
                splats = int(row["num_gaussians"])
                payload_bytes = int(row["payload_bytes"])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"{path}:{line_number}: invalid arrival series field"
                ) from error
            if arrival_us < last_time or splats <= 0 or payload_bytes <= 0:
                raise ValueError(f"{path}:{line_number}: invalid arrival series ordering")
            last_time = arrival_us
            total += splats
            total_bytes += payload_bytes
            cumulative.append((arrival_us, total))
            bin_index = arrival_us // bin_us
            while len(bins) <= bin_index:
                bins.append(0)
                byte_bins.append(0)
            bins[bin_index] += splats
            byte_bins[bin_index] += payload_bytes
    return {
        "cumulative": cumulative,
        "bins": bins,
        "byte_bins": byte_bins,
        "bin_us": bin_us,
        "total_splats": total,
        "total_payload_bytes": total_bytes,
    }


def transport_metric_series(path: Path) -> dict[str, object]:
    """Return RTT-derived queue-delay estimates from transport telemetry.

    The estimate is smoothed RTT minus the minimum smoothed RTT observed in the
    same run. It therefore includes endpoint/transport effects and must not be
    interpreted as a direct qdisc sojourn-time measurement.
    """
    points: list[tuple[int, float]] = []
    samples: list[tuple[int, int]] = []
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"time_us", "rtt_us"}.issubset(reader.fieldnames):
            raise ValueError(f"{path}: transport metrics need time_us and rtt_us")
        last_time = -1
        for line_number, row in enumerate(reader, start=2):
            try:
                time_us = int(row["time_us"])
                rtt_us = int(row["rtt_us"])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"{path}:{line_number}: invalid transport metric field"
                ) from error
            if time_us < last_time or rtt_us <= 0:
                raise ValueError(f"{path}:{line_number}: invalid transport metric ordering")
            last_time = time_us
            samples.append((time_us, rtt_us))
    if not samples:
        raise ValueError(f"{path}: transport metrics are empty")
    minimum_rtt_us = min(rtt_us for _, rtt_us in samples)
    points = [
        (time_us, max(rtt_us - minimum_rtt_us, 0) / 1000.0)
        for time_us, rtt_us in samples
    ]
    return {"queue_delay_ms": points, "minimum_rtt_us": minimum_rtt_us}


def render_arrival_comparison(
    timelines: dict[str, Path],
    output: Path,
    *,
    duration_us: int,
    transport_metrics: dict[str, Path] | None = None,
) -> None:
    """Render splat progress, foreground throughput, and delay as an SVG."""
    if duration_us <= 0:
        raise ValueError("duration_us must be positive")
    if not timelines:
        raise ValueError("at least one timeline is required")
    if transport_metrics is not None and set(transport_metrics) != set(timelines):
        raise ValueError("transport metric labels must match timeline labels")
    palette = ("#d55e00", "#0072b2", "#009e73", "#cc79a7", "#e69f00", "#56b4e9")
    series = {name: timeline_series(path) for name, path in timelines.items()}
    metric_series = (
        {name: transport_metric_series(path) for name, path in transport_metrics.items()}
        if transport_metrics is not None
        else {}
    )
    maximum_total = max(int(value["total_splats"]) for value in series.values()) or 1
    maximum_rate = max(
        (max(value["bins"], default=0) for value in series.values()), default=1
    ) or 1
    maximum_throughput = max(
        (
            max(value["byte_bins"], default=0) * 8
            / (int(value["bin_us"]) / 1_000_000)
            / 1_000_000
            for value in series.values()
        ),
        default=1,
    ) or 1
    maximum_delay = max(
        (
            max((delay for _, delay in value["queue_delay_ms"]), default=0)
            for value in metric_series.values()
        ),
        default=1,
    ) or 1
    width = 1200
    left, right = 104, 36
    plot_width = width - left - right
    legend_columns = min(3, len(series))
    legend_rows = math.ceil(len(series) / legend_columns)
    legend_y = 57
    first_panel_y = 86 + legend_rows * 24
    panel_height = 155
    panel_gap = 55
    panel_specs: list[tuple[str, float, str, str, str]] = [
        ("cumulative", float(maximum_total), "Cumulative received splats", "splats", "millions"),
        ("rate", float(maximum_rate), "Splat arrival rate (1 s bins)", "splats/s", "millions"),
        (
            "throughput",
            float(maximum_throughput),
            "Foreground payload throughput (1 s receive bins)",
            "Mbit/s",
            "decimal",
        ),
    ]
    if metric_series:
        panel_specs.append(
            (
                "delay",
                float(maximum_delay),
                "Estimated queue delay (smoothed RTT − run minimum RTT)",
                "ms",
                "decimal",
            )
        )
    panel_origins = {
        key: first_panel_y + index * (panel_height + panel_gap)
        for index, (key, _, _, _, _) in enumerate(panel_specs)
    }
    last_origin = panel_origins[panel_specs[-1][0]]
    height = last_origin + panel_height + 64

    def x(time_us: int) -> float:
        return left + min(max(time_us, 0), duration_us) / duration_us * plot_width

    def y(value: float, maximum: float, origin: int) -> float:
        return origin + panel_height - value / maximum * panel_height

    def tick_label(value: float, style: str) -> str:
        if style == "millions":
            return f"{value / 1_000_000:.2f}M"
        if value >= 100:
            return f"{value:.0f}"
        if value >= 10:
            return f"{value:.1f}"
        return f"{value:.2f}"

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#222}.axis{font-size:12px}'
        '.title{font-size:20px;font-weight:600}.panel{font-size:14px;font-weight:600}'
        '.grid{stroke:#ddd;stroke-width:1}.axisline{stroke:#555;stroke-width:1}</style>',
        f'<text class="title" x="{left}" y="30">3DGS foreground delivery over shared Mininet bottleneck</text>',
    ]
    for key, maximum, title, unit, label_style in panel_specs:
        origin = panel_origins[key]
        svg.append(f'<text class="panel" x="{left}" y="{origin - 18}">{title}</text>')
        for tick in range(5):
            value = maximum * tick / 4
            yy = origin + panel_height - panel_height * tick / 4
            svg.extend([
                f'<line class="grid" x1="{left}" y1="{yy:.1f}" x2="{left + plot_width}" y2="{yy:.1f}"/>',
                f'<text class="axis" text-anchor="end" x="{left - 8}" y="{yy + 4:.1f}">{tick_label(value, label_style)}</text>',
            ])
        svg.extend([
            f'<line class="axisline" x1="{left}" y1="{origin + panel_height}" x2="{left + plot_width}" y2="{origin + panel_height}"/>',
            f'<text class="axis" transform="translate(20 {origin + panel_height / 2}) rotate(-90)" text-anchor="middle">{unit}</text>',
        ])
    seconds = math.ceil(duration_us / 1_000_000)
    for tick in range(0, seconds + 1, 5):
        xx = x(tick * 1_000_000)
        for key, _, _, _, _ in panel_specs:
            origin = panel_origins[key]
            svg.append(
                f'<line class="grid" x1="{xx:.1f}" y1="{origin}" x2="{xx:.1f}" y2="{origin + panel_height}"/>'
            )
        svg.append(
            f'<text class="axis" text-anchor="middle" x="{xx:.1f}" y="{last_origin + panel_height + 20}">{tick}</text>'
        )
    svg.append(
        f'<text class="axis" text-anchor="middle" x="{left + plot_width / 2}" '
        f'y="{last_origin + panel_height + 45}">time since subscriber start (s)</text>'
    )
    for index, (name, value) in enumerate(series.items()):
        color = palette[index % len(palette)]
        points = " ".join(
            f"{x(time_us):.1f},{y(splats, maximum_total, panel_origins['cumulative']):.1f}"
            for time_us, splats in value["cumulative"]
        )
        svg.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{points}"/>'
        )
        bins = value["bins"]
        rate_points = [(0, 0)]
        rate_points.extend(
            ((bin_index + 0.5) * int(value["bin_us"]), splats)
            for bin_index, splats in enumerate(bins)
        )
        svg.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="'
            + " ".join(
                f"{x(int(time_us)):.1f},{y(int(splats), maximum_rate, panel_origins['rate']):.1f}"
                for time_us, splats in rate_points
            )
            + '"/>'
        )
        throughput_points = [(0, 0.0)]
        throughput_points.extend(
            (
                (bin_index + 0.5) * int(value["bin_us"]),
                payload_bytes * 8
                / (int(value["bin_us"]) / 1_000_000)
                / 1_000_000,
            )
            for bin_index, payload_bytes in enumerate(value["byte_bins"])
        )
        svg.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="'
            + " ".join(
                f"{x(int(time_us)):.1f},{y(throughput, maximum_throughput, panel_origins['throughput']):.1f}"
                for time_us, throughput in throughput_points
            )
            + '"/>'
        )
        if name in metric_series:
            svg.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="1.8" points="'
                + " ".join(
                    f"{x(time_us):.1f},{y(delay, maximum_delay, panel_origins['delay']):.1f}"
                    for time_us, delay in metric_series[name]["queue_delay_ms"]
                )
                + '"/>'
            )
        legend_column = index % legend_columns
        legend_row = index // legend_columns
        legend_width = plot_width / legend_columns
        legend_x = left + legend_column * legend_width
        current_legend_y = legend_y + legend_row * 24
        svg.extend([
            f'<line x1="{legend_x}" y1="{current_legend_y}" x2="{legend_x + 28}" y2="{current_legend_y}" stroke="{color}" stroke-width="3"/>',
            f'<text class="axis" x="{legend_x + 36}" y="{current_legend_y + 4}">{html.escape(name)}</text>',
        ])
    svg.append("</svg>")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(svg) + "\n", encoding="utf-8")
