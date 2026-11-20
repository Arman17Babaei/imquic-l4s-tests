#!/usr/bin/env python3
"""Measure five distinct cold scene render/encode/VMAF runs as one wall clock."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import resource
from pathlib import Path
import subprocess
import sys
import time

from render_trace_reference import ROOT

SCENES = (
    ('train', 'nthu-selected-150mbps/user8_train.json'),
    ('room', 'nthu-selected/user2_room.json'),
    ('truck', 'nthu-selected/user4_truck.json'),
    ('playroom', 'nthu-selected/user1_playroom.json'),
    ('drjohnson', 'nthu-selected-150mbps/user2_drjohnson.json'),
)


def process_tree_usage(root: int, accumulated: dict | None = None) -> tuple[float, int]:
    """Sum CPU seconds and RSS of the current tree (RSS double-counts shared pages)."""
    rows = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / 'stat').read_text()
            fields = text[text.rfind(')') + 2:].split()
            rows[int(entry.name)] = (int(fields[1]),
                (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
                int(fields[21]) * os.sysconf('SC_PAGE_SIZE'), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned = {root}
    while True:
        expanded = owned | {pid for pid, row in rows.items() if row[0] in owned}
        if expanded == owned:
            break
        owned = expanded
    if accumulated is not None:
        # Keep the last sample from exited processes and continue tracking known
        # descendants if Chromium reparents them. Start time protects PID reuse.
        owned |= {pid for pid, start in accumulated if pid in rows and rows[pid][3] == start}
        for pid in owned:
            if pid in rows:
                accumulated[(pid, rows[pid][3])] = rows[pid][1]
    return (sum(accumulated.values()) if accumulated is not None else
            sum(rows[pid][1] for pid in owned if pid in rows)), sum(rows[pid][2] for pid in owned if pid in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('/opt/live4s/data'))
    parser.add_argument('--dist', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parallel-scenes', type=int, default=5)
    parser.add_argument('--render-workers', type=int, default=1)
    parser.add_argument('--fps', type=int, default=1)
    parser.add_argument('--width', type=int, default=320)
    parser.add_argument('--height', type=int, default=180)
    parser.add_argument('--timeout-seconds', type=float, default=300)
    parser.add_argument('--target-seconds', type=float, default=90)
    parser.add_argument('--lane-cpus',
                        help='optional semicolon-separated disjoint CPU lists, one per parallel scene')
    args = parser.parse_args()
    output = args.output.resolve()
    cpus = sorted(os.sched_getaffinity(0))
    if not 1 <= args.parallel_scenes <= min(5, len(cpus)):
        parser.error('parallel scenes must be between 1 and 5 and fit allowed CPUs')
    lanes = [cpus[len(cpus) * lane // args.parallel_scenes:len(cpus) * (lane + 1) // args.parallel_scenes]
             for lane in range(args.parallel_scenes)]
    if args.lane_cpus:
        try:
            lanes = [[int(cpu) for cpu in lane.split(',')] for lane in args.lane_cpus.split(';')]
        except ValueError:
            parser.error('lane CPUs must be comma-separated integers, lanes separated by semicolons')
        flattened = [cpu for lane in lanes for cpu in lane]
        if (len(lanes) != args.parallel_scenes or len(set(flattened)) != len(flattened) or
                not set(flattened).issubset(cpus) or any(not lane for lane in lanes)):
            parser.error('CPU lanes must be nonempty, disjoint subsets of allowed CPUs')
    if (min(args.fps, args.width, args.height, args.timeout_seconds, args.target_seconds,
            args.render_workers) <= 0 or args.render_workers > min(map(len, lanes))):
        parser.error('positive settings and at least one CPU per render worker are required')
    for scene, trace in SCENES:
        for path in (args.data / f'scenes/{scene}/manifest-v2.json', args.data / 'traces' / trace):
            if not path.is_file():
                parser.error(f'missing batch input: {path}')
    if not output.is_relative_to(ROOT / 'results') or output.exists():
        parser.error('output must be a new directory under results/')
    output.mkdir(parents=True)
    started = time.perf_counter()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    def run_lane(lane):
        assigned = lanes[lane]
        results = []
        for scene, trace in SCENES[lane::args.parallel_scenes]:
            command = ['taskset', '-c', ','.join(map(str, assigned)), sys.executable,
                str(ROOT / 'tools/l4s/benchmark_render_vmaf.py'),
                '--manifest', str((args.data / f'scenes/{scene}/manifest-v2.json').resolve()),
                '--trace', str((args.data / 'traces' / trace).resolve()),
                '--dist', str(args.dist.resolve()), '--ffmpeg', str(args.ffmpeg.resolve()),
                '--output', str(output / scene), '--duration', '20', '--fps', str(args.fps),
                '--width', str(args.width), '--height', str(args.height), '--gpu-mode', 'llvmpipe',
                '--threads', '2', '--render-workers', str(args.render_workers),
                '--timeout-seconds', str(args.timeout_seconds), '--target-seconds', str(args.target_seconds)]
            start = time.perf_counter()
            with (output / f'{scene}.log').open('w') as log:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            path = output / scene / 'benchmark.json'
            report = json.loads(path.read_text()) if path.exists() else {}
            results.append({'scene': scene, 'command': command, 'cpus': assigned,
                            'wall_seconds': time.perf_counter() - start, 'exit_code': completed.returncode,
                            'complete': report.get('complete', False), 'benchmark': report})
        return results
    peak_rss, peak_cpu_seconds = 0, 0.0
    samples = []
    cpu_samples = {}
    with ThreadPoolExecutor(max_workers=args.parallel_scenes) as executor:
        futures = [executor.submit(run_lane, lane) for lane in range(args.parallel_scenes)]
        while not all(future.done() for future in futures):
            cpu_seconds, rss = process_tree_usage(os.getpid(), cpu_samples)
            peak_rss, peak_cpu_seconds = max(peak_rss, rss), max(peak_cpu_seconds, cpu_seconds)
            samples.append({'wall_seconds': time.perf_counter() - started,
                            'tree_cpu_seconds': cpu_seconds, 'tree_rss_bytes': rss})
            time.sleep(0.5)
        results = [result for future in futures for result in future.result()]
    elapsed = time.perf_counter() - started
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = (usage_after.ru_utime + usage_after.ru_stime -
                   usage_before.ru_utime - usage_before.ru_stime)
    summary = {'complete': len(results) == 5 and all(result['complete'] for result in results),
               'wall_seconds': elapsed, 'target_seconds': args.target_seconds,
               'parallel_scenes': args.parallel_scenes, 'render_workers_per_scene': args.render_workers,
               'fps': args.fps, 'viewport': [args.width, args.height], 'duration_seconds_per_scene': 20,
               'cpu_affinity': cpus, 'peak_tree_rss_bytes': peak_rss,
               'cpu_lanes': lanes,
               'peak_observed_tree_cpu_seconds': peak_cpu_seconds,
               'completed_children_cpu_seconds': cpu_seconds,
               'average_busy_cores': cpu_seconds / elapsed,
               'sampled_cumulative_cpu_seconds': sum(cpu_samples.values()),
               'sampled_average_busy_cores': sum(cpu_samples.values()) / elapsed,
               'batch_harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'resource_note': 'RSS sums processes and double-counts shared pages; cumulative CPU preserves last samples but misses tails and short-lived processes',
               'cpu_accounting_note': 'getrusage may omit reparented Chromium descendants; average_busy_cores is not total utilization',
               'scope': 'five cold complete scenes + encode + encoding-fidelity VMAF', 'scenes': results}
    summary['target_met'] = summary['complete'] and elapsed < args.target_seconds
    (output / 'resources.json').write_text(json.dumps(samples, indent=2) + '\n')
    (output / 'batch.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({key: value for key, value in summary.items() if key != 'scenes'}, indent=2))


if __name__ == '__main__':
    main()
