#!/usr/bin/env python3
"""Time a cold trace render, MP4 encoding and full-frame CPU VMAF.

Without --candidate, measure encoding fidelity against the clean rendered PNGs.
With --candidate, measure the supplied video's fidelity; its generation is NOT
included in the reported wall time. Never substitute SSIM for missing VMAF.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from render_trace_reference import ROOT, main as render_reference, sha256


def render_parallel(args, output: Path, expected: int) -> None:
    cpus = sorted(os.sched_getaffinity(0))
    workers = args.render_workers
    if workers > min(len(cpus), expected):
        raise ValueError('render workers exceed available CPUs or frames')
    def run(index):
        start, end = expected * index // workers, expected * (index + 1) // workers
        assigned = cpus[len(cpus) * index // workers:len(cpus) * (index + 1) // workers]
        command = ['taskset', '-c', ','.join(map(str, assigned)), sys.executable,
                   str(ROOT / 'tools/l4s/render_trace_reference.py'), '--manifest', str(args.manifest.resolve()),
                   '--trace', str(output / 'trace.json'), '--dist', str(args.dist.resolve()),
                   '--output', str(output / f'shard-{index}'), '--fps', str(args.fps),
                   '--width', str(args.width), '--height', str(args.height), '--gpu-mode', args.gpu_mode,
                   '--headless', '--capture-only', '--frame-start', str(start), '--frame-end', str(end),
                   '--timeout-seconds', str(args.timeout_seconds)]
        with (output / f'shard-{index}.log').open('w') as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        return {'command': command, 'cpus': assigned, 'start': start, 'end': end}
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        shards = list(pool.map(run, range(workers)))
    render_seconds = time.perf_counter() - started
    render = output / 'render'
    directory = render / 'reference-frames'
    directory.mkdir(parents=True)
    frames = []
    from compare_player_sequence import load_frames
    for index, shard in enumerate(shards):
        source = output / f'shard-{index}/reference-frames'
        shard_summary = json.loads((source.parent / 'reference-summary.json').read_text())
        record = json.loads((source / 'frames.json').read_text())
        validated_frames = load_frames(source / 'frames.json')
        if (not shard_summary['complete'] or not record['complete'] or
                len(record['frames']) != shard['end'] - shard['start']):
            raise ValueError('incomplete render shard')
        for frame in validated_frames:
            slot = len(frames)
            if abs(frame['timestampMs'] - slot * 1000 / args.fps) > 1e-6:
                raise ValueError('shard timestamp grid mismatch')
            if frame['appliedGaussians'] != shard_summary['appliedGaussians']:
                raise ValueError('shard frame does not contain the complete scene')
            file = f'frame-{slot:06d}.png'
            os.link(source / frame['file'], directory / file)
            frames.append(dict(frame, index=slot, file=file))
    if len(frames) != expected:
        raise ValueError('merged render frame count mismatch')
    manifest = directory / 'frames.json'
    manifest.write_text(json.dumps({'complete': True, 'expectedFrames': expected,
                                   'requestedFps': args.fps, 'frames': frames}, indent=2) + '\n')
    from snapshot_video import encode_snapshot_video
    encode_started = time.perf_counter()
    video = encode_snapshot_video(manifest, render / 'reference.mp4', fps=args.fps)
    summary = {'complete': True, 'render_seconds': render_seconds,
               'encode_seconds': time.perf_counter() - encode_started,
               'total_seconds': time.perf_counter() - started, 'shards': shards, 'video': video}
    (render / 'reference-summary.json').write_text(json.dumps(summary, indent=2) + '\n')


def validate_candidate(binary: Path, candidate: Path, *, fps: int, width: int,
                       height: int, frames: int) -> None:
    probe = binary.with_name('ffprobe')
    if not probe.is_file():
        fallback = shutil.which('ffprobe')
        if fallback is None:
            raise FileNotFoundError('VMAF benchmark requires ffprobe beside FFmpeg or on PATH')
        probe = Path(fallback)
    result = json.loads(subprocess.run([
        str(probe), '-v', 'error', '-count_frames', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,nb_read_frames,r_frame_rate,avg_frame_rate,start_time,duration,color_space,color_range',
        '-of', 'json', str(candidate)], check=True, capture_output=True, text=True).stdout)
    stream = result['streams'][0]
    numerator, denominator = map(int, stream['r_frame_rate'].split('/'))
    average = stream.get('avg_frame_rate', stream['r_frame_rate'])
    average_numerator, average_denominator = map(int, average.split('/'))
    if (stream['width'] != width or stream['height'] != height or
            int(stream['nb_read_frames']) != frames or numerator / denominator != fps or
            not average_denominator or average_numerator / average_denominator != fps or
            float(stream.get('start_time', 0)) != 0 or
            ('duration' in stream and abs(float(stream['duration']) - frames / fps) > 1e-4)):
        raise ValueError('candidate must match dimensions, frame count, FPS and zero origin')


def bounded_trace(trace: list[dict], duration_ms: float, fps: int = 10) -> list[dict]:
    if (not math.isfinite(duration_ms) or duration_ms <= 0 or len(trace) < 2
            or trace[0]['timestamp_ms'] != 0
            or any(not math.isfinite(frame['timestamp_ms']) for frame in trace)
            or any(b['timestamp_ms'] <= a['timestamp_ms'] for a, b in zip(trace, trace[1:]))
            or trace[-1]['timestamp_ms'] < duration_ms):
        raise ValueError('trace must start at zero, be ordered and cover the requested duration')
    # Keep the exact boundary if present. Otherwise preserve all original
    # samples and place the boundary at the preceding pose. This affects only
    # the final sub-sample interval; never interpolate view matrices elementwise.
    from copy import deepcopy
    result = [deepcopy(frame) for frame in trace if frame['timestamp_ms'] < duration_ms]
    right = next(frame for frame in trace if frame['timestamp_ms'] >= duration_ms)
    if right['timestamp_ms'] == duration_ms:
        result.append(deepcopy(right))
    else:
        last_capture = (math.ceil(duration_ms * fps / 1000) - 1) * 1000 / fps
        if result[-1]['timestamp_ms'] < last_capture:
            raise ValueError('sparse trace boundary would change captured camera poses; supply an exact duration sample')
        result.append(deepcopy(result[-1]))
        result[-1]['timestamp_ms'] = duration_ms
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--dist', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True, help='FFmpeg built with libvmaf')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--duration', type=float, default=20)
    parser.add_argument('--fps', type=int, default=10)
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=540)
    parser.add_argument('--threads', type=int, default=8)
    parser.add_argument('--render-workers', type=int, default=1)
    parser.add_argument('--gpu-mode', choices=('software', 'hardware', 'llvmpipe', 'mesa-gl'), default='software')
    parser.add_argument('--target-seconds', type=float, default=120)
    parser.add_argument('--timeout-seconds', type=float, default=300)
    args = parser.parse_args()
    if (not math.isfinite(args.target_seconds) or not math.isfinite(args.timeout_seconds)
            or args.width % 2 or args.height % 2):
        parser.error('timeouts must be finite and YUV420 dimensions must be even')
    if min(args.fps, args.width, args.height, args.threads, args.target_seconds, args.timeout_seconds, args.render_workers) <= 0:
        parser.error('dimensions, FPS, threads and target must be positive')
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'results') or output.exists():
        parser.error('output must be a new directory under results/')
    trace = bounded_trace(json.loads(args.trace.read_text()), args.duration * 1000, args.fps)
    expected = math.ceil(trace[-1]['timestamp_ms'] * args.fps / 1000)
    binary = args.ffmpeg.resolve()
    filters = subprocess.run([binary, '-hide_banner', '-filters'], check=True, capture_output=True, text=True)
    if ' libvmaf ' not in filters.stdout:
        parser.error('selected FFmpeg lacks libvmaf')
    output.mkdir(parents=True)
    (output / 'trace.json').write_text(json.dumps(trace) + '\n')
    summary = {'complete': False, 'duration_seconds': args.duration, 'fps': args.fps,
               'viewport': [args.width, args.height], 'cpu': platform.processor(),
               'logical_cpus': os.cpu_count(), 'threads': args.threads,
               'cpu_affinity': sorted(os.sched_getaffinity(0)),
               'gpu_mode': args.gpu_mode,
               'mesa_thread_limit': os.environ.get('LP_NUM_THREADS'),
               'render_workers': args.render_workers,
               'scope': 'external candidate comparison' if args.candidate else 'render + encode + encoding-fidelity VMAF',
               'ffmpeg_sha256': sha256(binary),
               'ffmpeg_version': subprocess.run([binary, '-version'], capture_output=True, text=True, check=True).stdout}
    started = time.perf_counter()
    try:
        if args.render_workers > 1:
            render_parallel(args, output, expected)
        else:
            render_reference(['--manifest', str(args.manifest.resolve()), '--trace', str(output / 'trace.json'),
                          '--dist', str(args.dist.resolve()), '--output', str(output / 'render'),
                          '--fps', str(args.fps), '--width', str(args.width), '--height', str(args.height),
                          '--gpu-mode', args.gpu_mode, '--headless', '--timeout-seconds', str(args.timeout_seconds)])
        render = json.loads((output / 'render/reference-summary.json').read_text())
        summary.update({key: render[key] for key in ('render_seconds', 'encode_seconds', 'total_seconds')})
        candidate = args.candidate.resolve() if args.candidate else output / 'render/reference.mp4'
        validate_candidate(binary, candidate, fps=args.fps, width=args.width,
                           height=args.height, frames=expected)
        # Both streams use the same frame-grid time base. No resizing, duplicate
        # frames or VMAF subsampling; RGB is explicitly converted to BT.709 limited.
        command = [str(binary), '-hide_banner', '-loglevel', 'info', '-threads', '2', '-i', str(candidate),
                   '-framerate', str(args.fps), '-threads', '2', '-i',
                   str(output / 'render/reference-frames/frame-%06d.png'),
                   '-filter_complex_threads', '2', '-filter_complex',
                   f'[0:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[d];'
                   f'[1:v]settb=AVTB,setpts=PTS-STARTPTS,scale=in_range=full:out_range=limited:'
                   f'out_color_matrix=bt709,format=yuv420p[r];'
                   f'[d][r]libvmaf=n_threads={args.threads}:log_fmt=json:log_path=vmaf.json',
                   '-f', 'null', '-']
        summary['vmaf_command'] = command
        vmaf_started = time.perf_counter()
        with (output / 'vmaf.log').open('w') as log:
            subprocess.run(command, cwd=output, stdout=log, stderr=subprocess.STDOUT, check=True)
        summary['vmaf_seconds'] = time.perf_counter() - vmaf_started
        scores = json.loads((output / 'vmaf.json').read_text())
        if len(scores['frames']) != expected:
            raise ValueError(f'VMAF returned {len(scores["frames"])} frames; expected {expected}')
        summary.update(complete=True, frames=expected, mean_vmaf=scores['pooled_metrics']['vmaf']['mean'])
    except Exception as error:
        summary['failure'] = str(error)
        raise
    finally:
        summary['wall_seconds'] = time.perf_counter() - started
        summary['target_met'] = (summary['complete'] and summary['wall_seconds'] < args.target_seconds
                                 if not args.candidate else None)
        (output / 'benchmark.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
