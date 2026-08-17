#!/usr/bin/env python3
"""Prepare and render deterministic 3DGS object bundles for IMQUIC tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator

BUNDLE_MAGIC = b"3DGSB001"
BUNDLE_VERSION = 1
BUNDLE_HEADER = struct.Struct("<8sIIQ")
RECORD_HEADER = struct.Struct("<I")
THREEDGS_PINNED_REVISION = "f4b0806d056cf84e258eaae0f82d26278a5c4040"
CHUNK_SIZE = 1024


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def git_revision(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"cannot determine 3DGS revision in {path}: {error}") from error


def git_dirty(path: Path) -> bool:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=path,
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"cannot inspect 3DGS worktree in {path}: {error}") from error
    return bool(status.strip())


def require_dependency(path: Path, *, allow_unpinned: bool = False) -> dict[str, object]:
    path = Path(path).resolve()
    if not (path / "src" / "streaming").is_dir():
        raise RuntimeError(f"{path} is not a 3dgs_over_moq checkout")
    revision = git_revision(path)
    dirty = git_dirty(path)
    if not allow_unpinned and revision != THREEDGS_PINNED_REVISION:
        raise RuntimeError(
            f"3dgs_over_moq is at {revision}, expected {THREEDGS_PINNED_REVISION}; "
            "use --allow-unpinned-3dgs only for exploratory runs"
        )
    if not allow_unpinned and dirty:
        raise RuntimeError(
            "3dgs_over_moq has uncommitted changes; commit them or use "
            "--allow-unpinned-3dgs for an exploratory run"
        )
    return {"path": str(path), "revision": revision, "dirty": dirty}


def _activate_dependency(path: Path) -> None:
    source = str(Path(path).resolve() / "src")
    if source not in sys.path:
        sys.path.insert(0, source)


def cluster_metadata(payload: bytes) -> dict[str, object]:
    """Parse routing identity from a 3dgs_over_moq encoded cluster frame."""
    if len(payload) < 32:
        raise ValueError("3DGS frame shorter than 32-byte header")
    magic, version, track_len, group_len, object_id, _, payload_len, subgroup_id = (
        struct.unpack("<8I", payload[:32])
    )
    if magic != 0x47535033 or version != 1:
        raise ValueError("unsupported 3DGS cluster frame")
    if 32 + payload_len > len(payload) or track_len + group_len > payload_len:
        raise ValueError("truncated 3DGS cluster frame")
    offset = 32
    track_id = payload[offset:offset + track_len].decode("utf-8")
    offset += track_len
    group_id = payload[offset:offset + group_len].decode("utf-8")
    return {
        "track_id": track_id,
        "group_id": group_id,
        "subgroup_id": subgroup_id,
        "object_id": object_id,
    }


def write_bundle(path: Path, records: Iterable[bytes]) -> dict[str, int]:
    """Write length-prefixed records with a self-describing header."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    payload_bytes = 0
    with path.open("wb+") as stream:
        stream.write(BUNDLE_HEADER.pack(BUNDLE_MAGIC, BUNDLE_VERSION, 0, 0))
        for record in records:
            if not record:
                raise ValueError("3DGS bundle records must be non-empty")
            if len(record) > 0xFFFFFFFF:
                raise ValueError("3DGS object exceeds bundle record limit")
            stream.write(RECORD_HEADER.pack(len(record)))
            stream.write(record)
            count += 1
            payload_bytes += len(record)
        stream.seek(0)
        stream.write(BUNDLE_HEADER.pack(BUNDLE_MAGIC, BUNDLE_VERSION, count, payload_bytes))
    return {"objects": count, "payload_bytes": payload_bytes, "file_bytes": path.stat().st_size}


def read_bundle(path: Path) -> Iterator[bytes]:
    with Path(path).open("rb") as stream:
        header = stream.read(BUNDLE_HEADER.size)
        if len(header) != BUNDLE_HEADER.size:
            raise ValueError(f"{path}: truncated bundle header")
        magic, version, count, declared_bytes = BUNDLE_HEADER.unpack(header)
        if magic != BUNDLE_MAGIC or version != BUNDLE_VERSION:
            raise ValueError(f"{path}: unsupported 3DGS bundle")
        payload_bytes = 0
        for _ in range(count):
            encoded_len = stream.read(RECORD_HEADER.size)
            if len(encoded_len) != RECORD_HEADER.size:
                raise ValueError(f"{path}: truncated record length")
            (length,) = RECORD_HEADER.unpack(encoded_len)
            payload = stream.read(length)
            if len(payload) != length:
                raise ValueError(f"{path}: truncated record payload")
            payload_bytes += length
            yield payload
        if payload_bytes != declared_bytes:
            raise ValueError(
                f"{path}: payload byte count mismatch ({payload_bytes} != {declared_bytes})"
            )
        if stream.read(1):
            raise ValueError(f"{path}: trailing bytes after declared records")


def bundle_summary(path: Path) -> dict[str, object]:
    count = 0
    payload_bytes = 0
    for record in read_bundle(path):
        count += 1
        payload_bytes += len(record)
    return {
        "objects": count,
        "payload_bytes": payload_bytes,
        "file_bytes": Path(path).stat().st_size,
        "sha256": sha256_file(Path(path)),
    }


def _scene_records(cache_path: Path, dependency: Path) -> Iterator[bytes]:
    """Yield one complete scene in progressive LOD order.

    The dependency's subgroup convention is preserved: subgroup 0 first for
    every cluster/group, then subgroup 1, then subgroup 2. Within a subgroup,
    denser groups (higher publish_sequence) are emitted first. This makes a
    deadline-cut transfer deterministic and progressively useful while keeping
    the exact same object sequence across congestion-control modes.
    """
    _activate_dependency(dependency)
    import torch  # type: ignore
    from streaming.transport.protocol import encode_cluster  # type: ignore

    data = torch.load(cache_path, weights_only=False, map_location="cpu")
    lod_cache = data.get("lod_cache")
    if not isinstance(lod_cache, dict) or not lod_cache:
        raise RuntimeError(f"{cache_path}: cache does not contain lod_cache")

    for subgroup_id in (0, 1, 2):
        for cluster_id in sorted(lod_cache):
            groups = sorted(
                lod_cache[cluster_id], key=lambda group: group.publish_sequence, reverse=True
            )
            for group in groups:
                subgroup = next(
                    (candidate for candidate in group.subgroups
                     if candidate.subgroup_id == subgroup_id),
                    None,
                )
                if subgroup is None or subgroup.num_gaussians <= 0:
                    continue
                if subgroup.sh_rest is not None and subgroup.sh_dc is not None:
                    sh_coeffs = torch.cat(
                        [
                            subgroup.sh_dc.reshape(subgroup.num_gaussians, -1),
                            subgroup.sh_rest.reshape(subgroup.num_gaussians, -1),
                        ],
                        dim=1,
                    )
                elif subgroup.sh_dc is not None:
                    sh_coeffs = subgroup.sh_dc.reshape(subgroup.num_gaussians, -1)
                else:
                    sh_coeffs = None
                chunks = max(1, math.ceil(subgroup.num_gaussians / CHUNK_SIZE))
                for object_id in range(chunks):
                    start = object_id * CHUNK_SIZE
                    end = min(start + CHUNK_SIZE, subgroup.num_gaussians)
                    yield encode_cluster(
                        f"track-{int(cluster_id):04d}",
                        str(group.group_id),
                        subgroup_id=subgroup_id,
                        object_id=object_id,
                        num_gaussians=end - start,
                        means=subgroup.means[start:end],
                        opacities=subgroup.opacities[start:end],
                        sh_coeffs=sh_coeffs[start:end] if sh_coeffs is not None else None,
                        scales=subgroup.scales[start:end],
                        rotations=subgroup.rotations[start:end],
                    )


def build_scene_bundle(
    cache_path: Path,
    bundle_path: Path,
    dependency: Path,
    *,
    allow_unpinned: bool = False,
) -> dict[str, object]:
    dependency_info = require_dependency(dependency, allow_unpinned=allow_unpinned)
    summary = write_bundle(bundle_path, _scene_records(cache_path, dependency))
    return {
        **summary,
        "bundle_sha256": sha256_file(bundle_path),
        "cache": str(Path(cache_path).resolve()),
        "cache_sha256": sha256_file(Path(cache_path)),
        "dependency": dependency_info,
        "ordering": "subgroup-0-then-1-then-2; cluster-id; publish-sequence-descending",
        "chunk_gaussians": CHUNK_SIZE,
    }


def render_bundle(
    bundle_path: Path,
    cache_path: Path,
    trace_path: Path,
    output_dir: Path,
    dependency: Path,
    *,
    frame_index: int = 0,
    device: str = "cuda:0",
    width: int = 1920,
    height: int = 1080,
    allow_unpinned: bool = False,
) -> dict[str, object]:
    dependency_info = require_dependency(dependency, allow_unpinned=allow_unpinned)
    _activate_dependency(dependency)
    from streaming.transport.protocol import decode_cluster  # type: ignore
    from streaming.transport.client.cache import SplatCache  # type: ignore
    from streaming.transport.client.pipeline import RenderPipeline  # type: ignore
    from streaming.transport.client.viewport.trace import load_trace  # type: ignore
    import torch  # type: ignore

    data = torch.load(cache_path, weights_only=False, map_location="cpu")
    manifest = data.get("manifest")
    if manifest is None:
        raise RuntimeError(f"{cache_path}: cache does not contain manifest")
    frames = load_trace(trace_path)
    if not 0 <= frame_index < len(frames):
        raise ValueError(f"frame index {frame_index} outside trace with {len(frames)} frames")

    cache = SplatCache()
    objects = 0
    payload_bytes = 0
    gaussians = 0
    for payload in read_bundle(bundle_path):
        cluster = decode_cluster(payload)
        cache.put(cluster)
        objects += 1
        payload_bytes += len(payload)
        gaussians += int(cluster["num_gaussians"])

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = frames[frame_index]
    pipeline = RenderPipeline(
        cache=cache,
        device=device,
        manifest=manifest,
        output_dir=output_dir,
        image_width=width,
        image_height=height,
        render_enabled=True,
    )
    rendered_path = pipeline.render_frame(
        frame_idx=frame_index,
        view_matrix=frame["view_matrix"],
        fov=frame["fov"],
        camera_position=frame["camera_position"],
        bytes_received=payload_bytes,
        clusters_received=objects,
    )
    pipeline.close()
    png = output_dir / "frames" / "png" / f"frame_{frame_index:04d}.png"
    summary = {
        "objects": objects,
        "payload_bytes": payload_bytes,
        "decoded_gaussians": gaussians,
        "cache_entries": cache.num_entries,
        "frame_index": frame_index,
        "device": device,
        "width": width,
        "height": height,
        "rendered": png.is_file(),
        "frame_png": str(png),
        "writer_result": str(rendered_path) if rendered_path is not None else None,
        "dependency": dependency_info,
    }
    (output_dir / "render-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def image_psnr(reference: Path, candidate: Path) -> float | None:
    from PIL import Image  # type: ignore
    import numpy as np  # type: ignore

    ref = np.asarray(Image.open(reference).convert("RGB"), dtype=np.float64)
    got = np.asarray(Image.open(candidate).convert("RGB"), dtype=np.float64)
    if ref.shape != got.shape:
        raise ValueError(f"image shape mismatch: {ref.shape} != {got.shape}")
    mse = float(np.mean((ref - got) ** 2))
    if mse == 0:
        return None
    return 10.0 * math.log10((255.0 * 255.0) / mse)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("--cache", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, required=True)
    build.add_argument("--allow-unpinned-3dgs", action="store_true")

    summary = commands.add_parser("summary")
    summary.add_argument("bundle", type=Path)

    render = commands.add_parser("render")
    render.add_argument("--bundle", type=Path, required=True)
    render.add_argument("--cache", type=Path, required=True)
    render.add_argument("--trace", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--3dgs-dir", dest="three_dgs_dir", type=Path, required=True)
    render.add_argument("--frame-index", type=int, default=0)
    render.add_argument("--device", default="cuda:0")
    render.add_argument("--width", type=int, default=1920)
    render.add_argument("--height", type=int, default=1080)
    render.add_argument("--allow-unpinned-3dgs", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        result = build_scene_bundle(
            args.cache, args.output, args.three_dgs_dir,
            allow_unpinned=args.allow_unpinned_3dgs,
        )
    elif args.command == "summary":
        result = bundle_summary(args.bundle)
    else:
        result = render_bundle(
            args.bundle, args.cache, args.trace, args.output, args.three_dgs_dir,
            frame_index=args.frame_index, device=args.device,
            width=args.width, height=args.height,
            allow_unpinned=args.allow_unpinned_3dgs,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
