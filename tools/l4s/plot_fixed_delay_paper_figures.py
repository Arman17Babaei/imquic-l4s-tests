#!/usr/bin/env python3
"""Export one paper PDF per scene/metric from retained, unmodified observations.

Run from the repository root with results/venvs/3dgs/bin/python.
LaTeX owns all scene headings, panel labels, captions, and panel positioning.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
COLORS = {160: '#007C91', 320: '#B34F2B'}
THRESHOLD = 13.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'results/all-10reruns')
    parser.add_argument('--trace-metrics', type=Path,
                        default=ROOT / 'results/figures/fast-camera-fixed-delay-20261108')
    parser.add_argument('--output', type=Path, default=ROOT / 'results/paper-fixed-delay-figures')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    inputs = {}

    def read(path: Path) -> bytes:
        data = path.read_bytes()
        inputs[str(path.resolve())] = hashlib.sha256(data).hexdigest()
        return data

    def rows(path: Path) -> list[dict]:
        return list(csv.DictReader(read(path).decode().splitlines()))

    summary = json.loads(read(args.source / 'analysis/summary.json'))
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 8,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'pdf.fonttype': 42})
    plotted, statuses, outputs = [], [], []
    for scene in ('playroom', 'train'):
        selected = [(i, r) for i, r in enumerate(summary['runs'])
                    if r['path'] == 'ideal' and r['scene'] == scene]
        assert len(selected) == 20
        quality, goodput = {}, {}
        trace = None
        for delay in (160, 320):
            members = sorted([(i, r) for i, r in selected if r['delay_ms'] == delay],
                             key=lambda pair: pair[1]['rerun'])
            assert [r['rerun'] for _, r in members] == list(range(1, 11))
            q, g = [], []
            for index, run in members:
                assert run['duration_seconds'] == 19 and run['bandwidth_mbps'] == 50
                assert run['capture_status'] == run['quality_status'] == 'PASS'
                assert run['vmaf_grid'] == 'shared exact poses'
                assert [r['timestamp_ms'] for r in run['vmaf_rows']] == list(range(0, 19000, 200))
                current = read(args.source / 'inputs' / run['id'] / 'trace.json')
                if trace is None:
                    trace = current
                assert current == trace, 'All repeats must share the camera trace'
                q.append([r['vmaf'] for r in run['vmaf_rows']])
                bins = rows(args.source / 'analysis' / f'run-{index:04d}-browser-payload.csv')
                assert [float(r['seconds']) for r in bins] == list(range(19))
                assert all(float(r['duration_seconds']) == 1 for r in bins)
                assert all(float(r['legacy_completion_timestamp_events']) == 0 for r in bins)
                values = [float(r['browser_payload_mbps']) for r in bins]
                assert np.allclose(values, [float(r['payload_bytes']) * 8 / 1e6 for r in bins])
                assert np.isclose(np.mean(values), run['mean_browser_payload_mbps'])
                g.append(values)
                statuses.append({k: run[k] for k in ('id', 'delay_ms', 'rerun',
                                                     'capture_status', 'quality_status')})
            quality[delay], goodput[delay] = np.asarray(q), np.asarray(g)

        metrics_dir = args.trace_metrics / scene if scene == 'train' else args.trace_metrics
        provenance = json.loads(read(metrics_dir / 'provenance.json'))
        # Authenticate retained trace-derived metrics against their original inputs.
        for name, expected in provenance['input_files'].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected['sha256']
        rotation = rows(metrics_dir / 'camera-rotation.csv')
        coverage = rows(metrics_dir / 'first-time-3d-coverage.csv')
        assert len(rotation) == len(coverage) == 950
        sample_time = np.asarray([float(r['sample_seconds']) for r in coverage])
        assert np.allclose(sample_time, [float(r['sample_seconds']) for r in rotation])
        coverage_rate = np.asarray([float(r['new_initial_fov_percent_per_second']) for r in coverage])
        mask = coverage_rate >= THRESHOLD
        starts = np.flatnonzero(mask & ~np.r_[False, mask[:-1]])
        ends = np.flatnonzero(mask & ~np.r_[mask[1:], False])
        intervals = [(float(coverage[i]['start_seconds']), float(coverage[j]['end_seconds']))
                     for i, j in zip(starts, ends)]
        specifications = [
            ('vmaf', 'VMAF', np.arange(95) / 5, quality),
            ('delta-vmaf', 'ΔVMAF\n(160 − 320 ms)', np.arange(95) / 5,
             {'paired': quality[160] - quality[320]}),
            ('goodput', 'Payload goodput\n(Mbps)', np.arange(19), goodput),
            ('rotation', 'Rotation speed (°/s)', sample_time,
             {'trace': np.asarray([float(r['angular_speed_deg_s']) for r in rotation])}),
            ('coverage', 'First-time coverage\n(% initial FoV/s)', sample_time, {'trace': coverage_rate}),
        ]
        for metric, ylabel, times, series in specifications:
            fig, ax = plt.subplots(figsize=(3.3, 1.18), layout='constrained')
            for start, end in intervals:
                ax.axvspan(start, end, color='#E5B544', alpha=.20, lw=0)
            for condition, values in series.items():
                color = COLORS.get(condition, '#7051A3' if metric == 'delta-vmaf' else '#424B57')
                if values.ndim == 2:
                    q25, median, q75 = np.percentile(values, [25, 50, 75], axis=0)
                    if metric == 'goodput':
                        ax.stairs(median, np.arange(20), baseline=None, color=color, lw=1,
                                  label=f'{condition} ms')
                        ax.fill_between(np.arange(20), np.r_[q25, q25[-1]], np.r_[q75, q75[-1]],
                                        step='post', color=color, alpha=.18, lw=0)
                    else:
                        ax.plot(times, median, color=color, lw=1,
                                label=f'{condition} ms' if isinstance(condition, int) else None)
                        ax.fill_between(times, q25, q75, color=color, alpha=.18, lw=0)
                    for t, lo, mid, hi in zip(times, q25, median, q75):
                        plotted.append(dict(scene=scene, metric=metric, condition=condition,
                                            seconds=t, n=10, q25=lo, median=mid, q75=hi, value=''))
                else:
                    ax.plot(times, values, color=color, lw=1)
                    for t, value in zip(times, values):
                        plotted.append(dict(scene=scene, metric=metric, condition=condition,
                                            seconds=t, n=1, q25='', median='', q75='', value=value))
            if metric == 'vmaf':
                ax.set_ylim(0, 100)
                ax.legend(loc='lower right', ncol=2, fontsize=6, frameon=False)
            elif metric == 'delta-vmaf':
                ax.axhline(0, color='#555555', ls='--', lw=.6)
            elif metric == 'goodput':
                ax.axhline(50, color='#666666', ls='--', lw=.6)
                ax.set_ylim(0, 60)
            elif metric == 'coverage':
                ax.axhline(THRESHOLD, color='#8A6C1B', ls=':', lw=.7)
                ax.set_ylim(bottom=0)
            else:
                ax.set_ylim(bottom=0)
            ax.set_xlim(0, 19)
            ax.set_xticks([0, 5, 10, 15, 19])
            ax.set_xlabel('Time (s)', fontsize=7, labelpad=1)
            ax.set_ylabel(ylabel, fontsize=7, labelpad=2)
            ax.tick_params(labelsize=7, pad=1)
            ax.grid(axis='y', alpha=.2)
            name = f'{scene}-{metric}.pdf'
            fig.savefig(args.output / name)
            plt.close(fig)
            outputs.append(name)

    with (args.output / 'plotted-data.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(plotted[0]))
        writer.writeheader()
        writer.writerows(plotted)
    record = dict(source=str(args.source.resolve()), inputs_sha256=inputs, runs=statuses,
                  configured_delays_ms=[160, 320], configured_bandwidth_mbps=50,
                  summary='Pointwise median and q25/q75; delta paired by repeat before aggregation',
                  display_rescaling=False, quality_adjustments=False, interpolation=False,
                  coverage_threshold_percent_initial_fov_per_second=THRESHOLD,
                  limitations=['Coverage measures viewing directions; translation and occlusion are excluded.',
                               'Rotation and coverage use retained centered 0.2 s moving averages.'],
                  outputs={name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
                           for name in outputs})
    (args.output / 'provenance.json').write_text(json.dumps(record, indent=2) + '\n')
    print(f'Wrote {len(outputs)} single-metric PDFs to {args.output}')


if __name__ == '__main__':
    main()
