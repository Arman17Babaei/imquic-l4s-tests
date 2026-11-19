#!/usr/bin/env python3
"""RGB SSIM/PSNR of timestamp-matched clean captures using FFmpeg or CUDA."""
from __future__ import annotations

import argparse
from bisect import bisect_left
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess


def snapshot_coverage(manifest: dict, max_missing_fraction: float = 0.0) -> dict:
    """Assess loss without changing raw completeness or filling missing slots."""
    if not math.isfinite(max_missing_fraction) or not 0 <= max_missing_fraction < 1:
        raise ValueError('max_missing_fraction must be finite and in [0, 1)')
    frames = manifest.get('frames', [])
    count = len(frames)
    expected = manifest.get('expectedFrames', count if manifest.get('complete') else None)
    errors = []
    acceptance_errors = [str(error) for error in manifest.get('errors', [])
              if not re.fullmatch(r'queue overflow at slot \d+', str(error))]
    if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0 or count > expected:
        errors.append('missing or invalid expected frame count')
        expected = None
    if manifest.get('playbackComplete') is False:
        acceptance_errors.append('playback did not complete')
    slots = [frame.get('slot') for frame in frames]
    if any(slot is not None for slot in slots):
        if (any(not isinstance(slot, int) or isinstance(slot, bool) or slot < 0 or
                (expected is not None and slot >= expected) for slot in slots) or
                slots != sorted(set(slots))):
            errors.append('frame slots must be unique, in range and strictly increasing')
        elif expected is not None and 'missingSlots' in manifest:
            missing = sorted(set(range(expected)) - set(slots))
            if manifest['missingSlots'] != missing:
                errors.append('missingSlots disagrees with captured slots')
    elif expected is not None and 'missingSlots' in manifest:
        missing = manifest['missingSlots']
        if (not isinstance(missing, list) or any(not isinstance(slot, int) or slot < 0 or slot >= expected for slot in missing)
                or len(set(missing)) != expected - count):
            errors.append('missingSlots disagrees with frame count')
    missing_count = expected - count if expected is not None else None
    fraction = missing_count / expected if expected else None
    # An incomplete manifest with zero loss needs explicit evidence explaining it.
    unexplained = not manifest.get('complete') and missing_count == 0
    if unexplained:
        acceptance_errors.append('incomplete capture has no missing slots explaining incompleteness')
    accepted = bool(count and not errors and not acceptance_errors and fraction is not None and
                    fraction <= max_missing_fraction)
    return {'accepted': accepted, 'complete': bool(manifest.get('complete')),
            'expected_frames': expected, 'available_frames': count, 'missing_frames': missing_count,
            'coverage': count / expected if expected else None, 'missing_fraction': fraction,
            'max_missing_fraction': max_missing_fraction, 'structural_errors': errors,
            'acceptance_errors': acceptance_errors}


def load_frames(path: Path, *, allow_incomplete: bool = False):
    data = json.loads(path.read_text())
    if (not data.get('complete') and not allow_incomplete) or not data.get('frames'):
        raise ValueError(f'incomplete frame capture: {path}')
    previous = -1.0
    for index, frame in enumerate(data['frames']):
        timestamp = frame['timestampMs']
        if frame['index'] != index or not math.isfinite(timestamp) or timestamp <= previous:
            raise ValueError('frame indices/timestamps must be complete and strictly ordered')
        previous = timestamp
        pose = frame.get('cameraPose', {})
        for field in ('viewMatrix', 'position', 'up'):
            values = pose.get(field)
            if not isinstance(values, list) or not values or any(
                    not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
                raise ValueError(f'malformed camera {field}')
        for field in ('fov', 'aspect'):
            value = pose.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'malformed camera {field}')
        image = (path.parent / frame['file']).resolve()
        if not image.is_relative_to(path.parent.resolve()) or not image.is_file():
            raise ValueError(f'missing or unsafe frame path: {image}')
    return data['frames']


def concat_file(frames, manifest: Path, destination: Path):
    lines = ['ffconcat version 1.0']
    for frame in frames:
        image = str((manifest.parent / frame['file']).resolve()).replace("'", "'\\''")
        lines.append(f"file '{image}'")
    destination.write_text('\n'.join(lines) + '\n')


def metric_values(path: Path, name: str):
    values = {}
    for line in path.read_text().splitlines():
        index, value = re.search(r'\bn:(\d+)', line), re.search(r'\b' + name + r':([^\s]+)', line)
        if index and value:
            values[int(index.group(1)) - 1] = float(value.group(1))
    return values


def cuda_metrics(candidates, references, candidate: Path, reference: Path):
    """FFmpeg's overlapping 8x8/stride-4 RGB SSIM and full-image RGB PSNR.

    Definition: FFmpeg libavfilter/vf_ssim.c, ssim_end1. Integer-rounded
    constants and sample covariance match the 8-bit implementation. Process
    one image pair at a time to bound VRAM independently of capture length.
    """
    import numpy as np
    from PIL import Image
    import torch
    import torch.nn.functional as functional

    if not torch.cuda.is_available():
        raise RuntimeError('CUDA quality requested but PyTorch CUDA is unavailable')
    ssims, psnrs = {}, {}
    with torch.inference_mode():
        for i, (got, expected) in enumerate(zip(candidates, references)):
            images = []
            for manifest, frame in ((candidate, got), (reference, expected)):
                with Image.open(manifest.parent / frame['file']) as image:
                    if image.mode not in ('RGB', 'RGBA'):
                        raise ValueError('CUDA metrics require 8-bit RGB PNGs')
                    array = np.array(image.convert('RGB'), copy=True)
                images.append(torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0).to(
                    device='cuda', dtype=torch.float64))
            a, b = images
            if a.shape != b.shape or min(a.shape[-2:]) < 8:
                raise ValueError('metric images must match dimensions and be at least 8x8')
            # Pooling ignores incomplete edge blocks just as FFmpeg does.
            pool = lambda x: functional.avg_pool2d(x, 8, stride=4) * 64
            sa, sb = pool(a), pool(b)
            variance = pool(a*a + b*b)*64 - sa*sa - sb*sb
            covariance = pool(a*b)*64 - sa*sb
            c1 = int(.01*.01*255*255*64 + .5)
            c2 = int(.03*.03*255*255*64*63 + .5)
            numerator = (2*sa*sb+c1).float() * (2*covariance+c2).float()
            denominator = (sa*sa+sb*sb+c1).float() * (variance+c2).float()
            ssims[i] = (numerator/denominator).double().mean().item()
            mse = ((a-b)**2).mean().item()
            psnrs[i] = 10*math.log10(255**2/mse) if mse else math.inf
    return ssims, psnrs, {'device_type': 'cuda', 'device': torch.cuda.get_device_name(0),
                         'torch_version': torch.__version__, 'cuda_version': torch.version.cuda,
                         'batch_size': 1, 'ssim_window': 8, 'ssim_stride': 4}


def compare_sequence(candidate: Path, reference: Path, output: Path, *, backend: str = "cpu", allow_incomplete: bool = False, max_missing_fraction: float = 0.0):
    if backend not in ("cpu", "cuda"):
        raise ValueError(f"unknown quality backend: {backend}")
    allow_partial = allow_incomplete or max_missing_fraction > 0
    candidates, references = load_frames(candidate, allow_incomplete=allow_partial), load_frames(reference, allow_incomplete=allow_partial)
    coverage = snapshot_coverage(json.loads(candidate.read_text()), max_missing_fraction)
    reference_coverage = snapshot_coverage(json.loads(reference.read_text()), max_missing_fraction)
    if coverage['structural_errors'] or reference_coverage['structural_errors']:
        raise ValueError('; '.join(coverage['structural_errors'] + reference_coverage['structural_errors']))
    if len(candidates) != len(references):
        if not allow_partial or len(candidates) > len(references):
            raise ValueError('candidate/reference frame counts differ')
        # A shared full trace reference includes slots the best-effort capture missed.
        timestamps = [frame['timestampMs'] for frame in references]
        aligned = []
        for frame in candidates:
            index = bisect_left(timestamps, frame['timestampMs'] - 1e-6)
            if index == len(references) or abs(timestamps[index] - frame['timestampMs']) > 1e-6:
                raise ValueError('candidate/reference timestamps differ')
            aligned.append(references[index])
        references = aligned
    for got, expected in zip(candidates, references):
        if abs(got['timestampMs'] - expected['timestampMs']) > 1e-6:
            raise ValueError('candidate/reference timestamps differ')
        for field in ('viewMatrix', 'position', 'up'):
            a, b = got['cameraPose'][field], expected['cameraPose'][field]
            if len(a) != len(b) or any(abs(x - y) > 1e-4 for x, y in zip(a, b)):
                raise ValueError(f'candidate/reference camera {field} differs')
        for field in ('fov', 'aspect'):
            if abs(got['cameraPose'][field] - expected['cameraPose'][field]) > 1e-6:
                raise ValueError(f'candidate/reference camera {field} differs')
    output.mkdir(parents=True, exist_ok=True)
    if backend == 'cuda':
        ssims, psnrs, backend_info = cuda_metrics(candidates, references, candidate, reference)
    else:
        backend_info = {'device_type': 'cpu', 'implementation': 'FFmpeg'}
        concat_file(candidates, candidate, output / 'candidate.ffconcat')
        concat_file(references, reference, output / 'reference.ffconcat')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'concat', '-safe', '0', '-i', str((output / 'candidate.ffconcat').resolve()),
            '-f', 'concat', '-safe', '0', '-i', str((output / 'reference.ffconcat').resolve()),
            '-filter_complex', '[0:v]format=gbrp,split[c0][c1];[1:v]format=gbrp,split[r0][r1];'
                '[c0][r0]ssim=stats_file=ssim.log[s];[c1][r1]psnr=stats_file=psnr.log[p]',
            '-map', '[s]', '-map', '[p]', '-f', 'null', '-'], cwd=output, check=True)
        ssims, psnrs = metric_values(output / 'ssim.log', 'All'), metric_values(output / 'psnr.log', 'psnr_avg')
    if set(ssims) != set(range(len(candidates))) or set(psnrs) != set(ssims):
        raise ValueError('FFmpeg did not compare exactly one metric pair per capture')
    rows = [{'frame': i, 'timestamp_ms': frame['timestampMs'], 'rgb_ssim': ssims[i],
             'psnr_db': psnrs[i], 'applied_gaussians': frame['appliedGaussians']}
            for i, frame in enumerate(candidates)]
    with (output / 'frame-quality.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    # No arbitrary quality PASS threshold: successful evaluation is distinct
    # from a scientific claim about the transport or visual quality.
    finite_psnr = [value for value in psnrs.values() if math.isfinite(value)]
    accepted = coverage['accepted'] and reference_coverage['accepted']
    result = {'status': 'PASS' if accepted else 'INCOMPLETE', 'accepted': accepted,
        'snapshot_coverage': coverage, 'reference_snapshot_coverage': reference_coverage, 'reference': 'same player, complete scene, identical trace timestamps and camera',
        'metric': 'FFmpeg-compatible RGB SSIM (8x8, stride 4) and PSNR on clean PNGs, before video encoding',
        'backend': backend_info,
        'frames': len(rows), 'mean_ssim': sum(ssims.values()) / len(rows), 'min_ssim': min(ssims.values()),
        'mean_finite_psnr_db': sum(finite_psnr) / len(finite_psnr) if finite_psnr else None,
        'infinite_psnr_frames': len(rows) - len(finite_psnr),
        'candidate_manifest_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
        'reference_manifest_sha256': hashlib.sha256(reference.read_bytes()).hexdigest()}
    (output / 'comparison.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--max-missing-fraction', type=float, default=0.0)
    parser.add_argument('--allow-incomplete', action='store_true')
    args = parser.parse_args()
    try:
        result = compare_sequence(args.candidate.resolve(), args.reference.resolve(), args.output.resolve(), backend=args.backend, allow_incomplete=args.allow_incomplete, max_missing_fraction=args.max_missing_fraction)
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.CalledProcessError) as error:
        result = {'status': 'FAIL', 'reason': str(error)}
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
