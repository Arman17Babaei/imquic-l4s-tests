#!/usr/bin/env python3
"""Export source PLY Gaussians as hashed SH3 GSP2 objects for the priority player."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

import numpy as np
from plyfile import PlyData


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def encode_object(vertices, indices, tile: str, layer: int, object_id: int) -> bytes:
    count = len(indices)
    def columns(names):
        return np.column_stack([vertices[name][indices] for name in names]).astype('<f4')
    means = columns(['x', 'y', 'z'])
    opacity = np.asarray(vertices['opacity'][indices], dtype='<f4')
    dc = columns([f'f_dc_{i}' for i in range(3)])
    rest = columns([f'f_rest_{i}' for i in range(45)]).reshape(count, 3, 15).transpose(0, 2, 1)
    sh = np.concatenate([dc[:, None, :], rest], axis=1).astype('<f4')
    scales = columns([f'scale_{i}' for i in range(3)])
    rotations = columns([f'rot_{i}' for i in range(4)])
    arrays = [np.asarray(indices, dtype='<u4'), means, opacity, sh, scales, rotations]
    if any(not np.isfinite(array).all() for array in arrays):
        raise ValueError('non-finite PLY Gaussian data')
    track, group = tile.encode(), b'0'
    payload = track + group + b''.join(struct.pack('<I', array.nbytes) + array.tobytes() for array in arrays)
    return struct.pack('<10I', 0x47535033, 2, 0 if layer == 0 else 1, len(track), len(group),
                       layer, object_id, count, 3, len(payload)) + payload


def export_scene(source: Path, output: Path, grid=(10, 10, 3), fractions=(.1, .2, .3, .4), chunk=256, max_tile_gaussians: int | None = None):
    if output.exists():
        raise ValueError('output must not exist; preserve earlier evidence')
    vertices = PlyData.read(source, mmap='r')['vertex'].data
    if not len(vertices) or not 1 <= chunk <= 256:
        raise ValueError('nonempty scene and chunk size 1..256 required (64 KiB object budget)')
    bounds_min = np.array([vertices[axis].min() for axis in ('x', 'y', 'z')], dtype=np.float64)
    bounds_max = np.array([vertices[axis].max() for axis in ('x', 'y', 'z')], dtype=np.float64)
    cell_id = np.zeros(len(vertices), dtype=np.int32)
    for axis, name in enumerate(('x', 'y', 'z')):
        span = bounds_max[axis] - bounds_min[axis]
        coord = np.zeros(len(vertices), dtype=np.int32) if span == 0 else np.minimum(
            grid[axis] - 1, np.floor((vertices[name].astype(np.float64) - bounds_min[axis]) / span * grid[axis])).astype(np.int32)
        cell_id = cell_id * grid[axis] + coord
    order = np.argsort(cell_id, kind='stable')
    boundaries = np.r_[0, np.flatnonzero(np.diff(cell_id[order])) + 1, len(order)]
    partitions = []
    def partition(indices, cell):
        if max_tile_gaussians is None or len(indices) <= max_tile_gaussians:
            partitions.append((indices, cell)); return
        extent = np.array([np.ptp(vertices[name][indices]) for name in ('x', 'y', 'z')])
        axis = ('x', 'y', 'z')[int(np.argmax(extent))]
        ordered = indices[np.argsort(vertices[axis][indices], kind='stable')]
        middle = len(ordered) // 2
        partition(ordered[:middle], cell); partition(ordered[middle:], cell)
    if max_tile_gaussians is not None and max_tile_gaussians < 1:
        raise ValueError('positive tile Gaussian limit required')
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        partition(order[start:end], int(cell_id[order[start]]))
    output.mkdir(parents=True)
    tiles = []
    cumulative = np.cumsum(fractions)
    for tile_index, (indices, cell) in enumerate(partitions):
        indices = indices[np.argsort(-vertices['opacity'][indices], kind='stable')]
        tile_id = f'tile-{tile_index:03d}'
        means = np.column_stack([vertices[name][indices] for name in ('x', 'y', 'z')])
        low, high = means.min(axis=0), means.max(axis=0)
        grid_index = [cell // (grid[1] * grid[2]), (cell // grid[2]) % grid[1], cell % grid[2]]
        ends = [int(np.floor(len(indices) * fraction + .5)) for fraction in cumulative]
        ends[-1] = len(indices)
        layers = []
        previous = 0
        for layer, layer_end in enumerate(ends):
            selected = indices[previous:layer_end]; previous = layer_end
            file_name = f'{tile_id}_refinement_{layer}.gsp2'
            descriptions = []; offset = 0; digest = hashlib.sha256()
            object_count = (len(selected) + chunk - 1) // chunk
            with (output / file_name).open('wb') as stream:
                for object_id in range(object_count):
                    part = selected[object_id * chunk:(object_id + 1) * chunk]
                    encoded = encode_object(vertices, part, tile_id, layer, object_id)
                    if len(encoded) > 65536:
                        raise ValueError('GSP2 object exceeds the priority transport object budget')
                    stream.write(encoded); digest.update(encoded)
                    descriptions.append({'id': object_id, 'offset': offset, 'bytes': len(encoded),
                        'gaussian_count': len(part), 'publisher_priority': round(255 * object_id / max(1, object_count - 1)),
                        'sha256': hashlib.sha256(encoded).hexdigest()})
                    offset += len(encoded)
            layers.append({'index': layer, 'name': 'base' if layer == 0 else f'enhancement{layer}',
                'file': file_name, 'track': f'bicycle/{tile_id}/refinement/{layer}', 'gaussian_count': len(selected),
                'bytes': offset, 'object_count': object_count, 'object_bytes': max((o['bytes'] for o in descriptions), default=0),
                'objects': descriptions, 'sha256': digest.hexdigest(), 'fraction': fractions[layer]})
        tiles.append({'id': tile_id, 'grid_index': grid_index, 'min': low.tolist(), 'max': high.tolist(),
            'centroid': ((low + high) / 2).tolist(), 'gaussian_count': len(indices), 'refinements': layers})
    center = (bounds_min + bounds_max) / 2
    manifest = {'schema_version': 2, 'payload_encoding': 'gsp2', 'coordinate_system': 'source',
        'scene': {'id': 'bicycle', 'source_sha256': sha256_file(source), 'source_gaussian_count': len(vertices),
            'sampling': 'full', 'splat_size': 240, 'gaussian_count': len(vertices), 'min': bounds_min.tolist(),
            'max': bounds_max.tolist(), 'center': center.tolist(), 'radius': float(np.linalg.norm(bounds_max - bounds_min) / 2)},
        'tiling': {'type': 'adaptive-aabb-grid' if max_tile_gaussians else 'uniform-aabb-grid', 'dimensions': list(grid), 'occupied_tiles': len(tiles), 'max_gaussians_per_tile': max_tile_gaussians},
        'progression': {'ordering': 'opacity-descending-stable-source-order', 'layer_fractions': list(fractions),
            'cumulative_fractions': cumulative.tolist()}, 'tiles': tiles}
    (output / 'manifest-v2.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chunk-gaussians', type=int, default=256)
    parser.add_argument('--max-tile-gaussians', type=int)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(Path(__file__).resolve().parents[2] / 'results'):
        parser.error('output must be under results/')
    export_scene(args.source, args.output, chunk=args.chunk_gaussians, max_tile_gaussians=args.max_tile_gaussians)


if __name__ == '__main__':
    main()
