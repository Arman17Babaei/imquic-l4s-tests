#!/usr/bin/env python3
"""CPU RGB SSIM/PSNR of timestamp-matched clean player captures using FFmpeg."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess


def load_frames(path: Path):
    data = json.loads(path.read_text())
    if not data.get('complete') or not data.get('frames'):
        raise ValueError(f'incomplete frame capture: {path}')
    previous = -1.0
    for index, frame in enumerate(data['frames']):
        timestamp = frame['timestampMs']
        if frame['index'] != index or not math.isfinite(timestamp) or timestamp <= previous:
            raise ValueError('frame indices/timestamps must be complete and strictly ordered')
        previous = timestamp
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


def compare_sequence(candidate: Path, reference: Path, output: Path):
    candidates, references = load_frames(candidate), load_frames(reference)
    if len(candidates) != len(references):
        raise ValueError('candidate/reference frame counts differ')
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
    result = {'status': 'PASS', 'reference': 'same player, complete scene, identical trace timestamps and camera',
        'metric': 'FFmpeg RGB/gbrp SSIM and PSNR on clean PNGs, before video encoding',
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
    args = parser.parse_args()
    try:
        result = compare_sequence(args.candidate.resolve(), args.reference.resolve(), args.output.resolve())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        result = {'status': 'FAIL', 'reason': str(error)}
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
