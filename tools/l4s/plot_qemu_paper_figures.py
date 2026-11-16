#!/usr/bin/env python3
"""Export separate QEMU scene/metric PDFs; LaTeX owns panel composition.

Reproduce with results/venvs/3dgs/bin/python tools/l4s/plot_qemu_paper_figures.py.
Uses original retained quality and browser payload receipt bins.
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
COLORS = {'prague': '#007C91', 'reno': '#B34F2B'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'results/all-10reruns')
    parser.add_argument('--output', type=Path, default=ROOT / 'results/paper-qemu-figures')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    inputs, statuses, plotted, outputs = {}, [], [], []

    def read(path: Path) -> bytes:
        raw = path.read_bytes()
        inputs[str(path.resolve())] = hashlib.sha256(raw).hexdigest()
        return raw

    summary = json.loads(read(args.source / 'analysis/summary.json'))
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 8,
                         'axes.spines.top': False, 'axes.spines.right': False, 'pdf.fonttype': 42})
    for scene in ('playroom', 'train'):
        series = {metric: {} for metric in ('vmaf', 'goodput')}
        for controller in COLORS:
            members = sorted([(i, r) for i, r in enumerate(summary['runs'])
                              if r['path'] == 'qemu' and r['scene'] == scene
                              and r['controller'] == controller], key=lambda pair: pair[1]['rerun'])
            assert [r['rerun'] for _, r in members] == list(range(1, 11))
            quality, goodput = [], []
            for index, run in members:
                assert run['duration_seconds'] == 19 and run['bandwidth_mbps'] == 50
                assert run['delay_ms'] == 10 and run['qemu_delay_mode'] == 'per-link'
                assert run['switches'] == 2 and run['background'] and run['background_cc'] == 'cubic'
                assert run['capture_status'] == run['quality_status'] == 'PASS'
                assert run['vmaf_grid'] == 'shared exact poses'
                assert [v['timestamp_ms'] for v in run['vmaf_rows']] == list(range(0, 19000, 200))
                quality.append([v['vmaf'] for v in run['vmaf_rows']])
                bins = list(csv.DictReader(read(args.source / 'analysis' /
                                               f'run-{index:04d}-browser-payload.csv').decode().splitlines()))
                assert [float(b['seconds']) for b in bins] == list(range(19))
                assert all(float(b['duration_seconds']) == 1 for b in bins)
                assert all(float(b['legacy_completion_timestamp_events']) == 0 for b in bins)
                payload = np.asarray([float(b['payload_bytes']) for b in bins])
                values = np.asarray([float(b['browser_payload_mbps']) for b in bins])
                assert np.allclose(values, payload * 8 / 1e6)
                goodput.append(values)
                statuses.append({key: run[key] for key in ('id', 'scene', 'controller', 'rerun',
                                                           'capture_status', 'quality_status')})
            for metric, values in zip(series, (quality, goodput)):
                series[metric][controller] = np.asarray(values)
        for metric, ylabel in [('vmaf', 'VMAF'), ('goodput', 'Payload goodput\n(Mbps)')]:
            fig, ax = plt.subplots(figsize=(3.3, 1.35), layout='constrained')
            times = np.arange(95) / 5 if metric == 'vmaf' else np.arange(19) + .5
            for controller, values in series[metric].items():
                q25, median, q75 = np.percentile(values, [25, 50, 75], axis=0)
                color = COLORS[controller]
                if metric == 'vmaf':
                    ax.plot(times, median, color=color, lw=1, label=controller.title())
                    ax.fill_between(times, q25, q75, color=color, alpha=.18, lw=0)
                else:
                    ax.stairs(median, np.arange(20), color=color, lw=1, baseline=None,
                              label=controller.title())
                    ax.fill_between(np.arange(20), np.r_[q25, q25[-1]], np.r_[q75, q75[-1]],
                                    color=color, alpha=.18, lw=0, step='post')
                for t, lo, mid, hi in zip(times, q25, median, q75):
                    plotted.append(dict(scene=scene, metric=metric, controller=controller,
                                        seconds=t, n=10, q25=lo, median=mid, q75=hi))
            ax.set_xlim(0, 19)
            ax.set_ylim(bottom=0)
            if metric == 'vmaf':
                ax.set_ylim(0, 100)
                ax.legend(loc='upper left', ncol=2, fontsize=6, frameon=False)
            elif metric == 'goodput':
                ax.axhline(50, color='#666666', ls='--', lw=.6)
                ax.set_ylim(0, 60)
            ax.set_xticks([0, 5, 10, 15, 19])
            ax.set_xlabel('Time (s)', fontsize=7, labelpad=1)
            ax.set_ylabel(ylabel, fontsize=7, labelpad=2)
            ax.tick_params(labelsize=7, pad=1)
            ax.grid(axis='y', alpha=.2)
            name = f'qemu-{scene}-{metric}.pdf'
            fig.savefig(args.output / name)
            plt.close(fig)
            outputs.append(name)
    with (args.output / 'plotted-data.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(plotted[0]))
        writer.writeheader()
        writer.writerows(plotted)
    record = dict(inputs_sha256=inputs, runs=statuses, configured_bandwidth_mbps=50,
                  configured_per_link_delay_ms=10, competing_controller='cubic',
                  statistics='Median and q25/q75 across 10 repeats per scene/controller',
                  interpolation=False, quality_adjustments=False, display_rescaling=False,
                  limitations=['Observed end-to-end differences combine bandwidth and latency effects.'],
                  outputs={name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
                           for name in outputs})
    (args.output / 'provenance.json').write_text(json.dumps(record, indent=2) + '\n')
    print(f'Wrote {len(outputs)} single-metric QEMU PDFs from {len(statuses)} runs to {args.output}')


if __name__ == '__main__':
    main()
