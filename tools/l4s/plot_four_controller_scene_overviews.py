"""Render retained four-controller comparisons; no network experiments are launched."""
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
from PIL import Image

COLORS = {'prague': '#007C91', 'reno': '#B34F2B', 'reno-ect0': '#8D5AA5', 'bbr': '#AD8300',
          'ideal0': '#333333', 'ideal50': '#777777'}
LABELS = {'prague': 'Prague', 'reno': 'Reno Not-ECT', 'reno-ect0': 'Reno ECT(0)', 'bbr': 'BBR'}
CONTROLLERS = ['prague', 'bbr', 'reno-ect0', 'reno']
TIMES = [5000, 10000, 15000]
METRICS = [('vmaf', 'VMAF'), ('goodput', 'QUIC stream throughput (Mbps)'),
           ('rtt', 'Smoothed RTT (ms)'), ('cwnd', 'cwnd (KiB)'),
           ('ce', 'Sum CE (packets / 100 ms)'), ('loss', 'Sum loss (packets / 100 ms)')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scene', required=True)
    args = parser.parse_args()
    out = args.output.resolve();out.mkdir(parents=True, exist_ok=True)
    source = args.source.resolve();scene = args.scene
    hashes = {}

    def read(path):
        raw = path.read_bytes();hashes[str(path.resolve())] = hashlib.sha256(raw).hexdigest()
        return raw

    def jread(path):
        return json.loads(read(path))

    ledger = jread(source / 'ledger.json')
    summary = jread(source / 'analysis/summary.json')
    items = {i['id']: i for i in ledger['items']}
    runs = [r for r in summary['runs'] if r['scene'] == scene]
    qemu = [r for r in runs if r['path'] == 'qemu']
    ideals = {r['delay_ms']: r for r in runs if r['path'] == 'ideal'}
    assert len(qemu) == 20
    assert all([r['timestamp_ms'] for r in run['vmaf_rows']] == [r['timestamp_ms'] for r in qemu[0]['vmaf_rows']] for run in qemu)
    assert all(r['bandwidth_mbps'] == 150 and r['delay_ms'] == 10 and r['vmaf_status'] == 'PASS' for r in qemu)
    for controller in CONTROLLERS:
        assert sorted(r['rerun'] for r in qemu if r['controller'] == controller) == [1, 2, 3, 4, 5]
    if ideals:
        assert set(ideals) == {0, 1, 5, 10, 50, 100, 500}
        assert all(r['bandwidth_mbps'] == 150 and r['vmaf_status'] == 'PASS' for r in ideals.values())
    partial = [r['id'] for r in qemu if r['source_attempt_status'] != 'PASS']

    def directory(run):
        item = items[run['id']]
        attempt = next(a for a in item['attempts'] if a['index'] == item['selected_attempt'])
        return source / attempt['directory']

    def frame(run, kind, timestamp):
        folder = directory(run) / f'{kind}-frames'
        manifest = jread(folder / 'frames.json')
        rows = [r for r in manifest['frames'] if r['timestampMs'] == timestamp]
        assert len(rows) <= 1
        if not rows:
            return None
        row = rows[0];path = folder / row['file']
        if not path.exists():return None
        read(path)
        return row, path, Image.open(path).convert('RGB')

    reference_run = next(r for r in qemu if r['controller'] == 'prague' and r['rerun'] == 1)
    references = [frame(reference_run, 'reference', t) for t in TIMES]
    assert all(references)
    conditions = []
    if ideals:
        conditions += [('ideal0', 'Ideal\n0 ms one-way', ideals[0]), ('ideal50', 'Ideal\n50 ms one-way', ideals[50])]
    conditions += [(c, LABELS[c] + '\n20 ms RTT', next(r for r in qemu if r['controller'] == c and r['rerun'] == 1)) for c in CONTROLLERS]
    pictures = [('reference', 'Full reference', [r[2] for r in references])]
    evidence = []
    for key, label, run in conditions:
        images = []
        for index, timestamp in enumerate(TIMES):
            candidate = frame(run, 'candidate', timestamp)
            reference = frame(run, 'reference', timestamp)
            assert reference and reference[0]['cameraPose'] == references[index][0]['cameraPose']
            assert reference[2].tobytes() == references[index][2].tobytes()
            assert reference[0]['appliedGaussians'] == references[index][0]['appliedGaussians']
            if candidate:
                assert candidate[0]['cameraPose'] == reference[0]['cameraPose']
            images.append(candidate[2] if candidate else None)
            evidence.append({'condition': key, 'id': run['id'], 'timestamp_ms': timestamp,
                             'candidate': str(candidate[1]) if candidate else None,
                             'reference': str(reference[1]), 'missing': candidate is None,
                             'candidate_all_black': candidate[2].getbbox() is None if candidate else None,
                             'camera_pose_equal': bool(candidate), 'reference_rgb_equal': True})
        pictures.append((key, label, images))
    stats = list(csv.DictReader(read(out / 'transport/plotted-data.csv').decode().splitlines()))
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 7.5,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.linewidth': .6, 'pdf.fonttype': 42, 'svg.fonttype': 'none'})
    width, height = 210 / 25.4, 297 / 25.4
    fig = plt.figure(figsize=(width, height), facecolor='white')
    display = {'drjohnson': 'Dr Johnson'}.get(scene, scene.title())
    fig.text(.055, .975, display, fontsize=13, weight='bold', va='top')
    fig.text(.055, .951, '150 Mbps | QEMU: 20 ms base RTT | Cubic + Prague TCP backgrounds | 5 repeats/controller',
             fontsize=7.4, va='top', color='#444444')
    image_width = .138 if ideals else .205
    image_height = image_width * width / height * 9 / 16
    image_left = .245
    centers = [.325, .565, .805]
    for center, timestamp in zip(centers, TIMES):
        fig.text(center, .925, f'{timestamp / 1000:g} s', ha='center', fontsize=9)
    for row, (key, label, images) in enumerate(pictures):
        bottom = .910 - row * (image_height + .007) - image_height
        fig.text(.055, bottom + image_height / 2, label, va='center', fontsize=7.3,
                 color=COLORS.get(key, '#222222'))
        for center, image in zip(centers, images):
            ax = fig.add_axes([center-image_width/2, bottom, image_width, image_height])
            if image is None:
                ax.set_facecolor('#eeeeee');ax.text(.5, .5, 'Missing snapshot', ha='center', va='center', fontsize=6)
                ax.set_xticks([]);ax.set_yticks([])
            else:
                ax.imshow(image, interpolation='none');ax.set_axis_off()
    for index, (metric, label) in enumerate(METRICS):
        row, col = divmod(index, 2)
        ax = fig.add_axes([.085 + col * .49, .387 - row * .107, .385, .065])
        for controller in CONTROLLERS:
            rows = [r for r in stats if r['metric'] == metric and r['controller'] == controller]
            times = [float(r['time_s']) for r in rows]
            if metric in ('ce', 'loss'):
                values = [float(r['sum']) for r in rows]
            else:
                values = [float(r['median']) if r['median'] else np.nan for r in rows]
                low = [float(r['q25']) if r['q25'] else np.nan for r in rows]
                high = [float(r['q75']) if r['q75'] else np.nan for r in rows]
                ax.fill_between(times, low, high, color=COLORS[controller], alpha=.13, lw=0)
            ax.plot(times, values, color=COLORS[controller], lw=.65, label=LABELS[controller])
        if metric == 'vmaf':
            for delay in ([0, 50] if ideals else []):
                series = ideals[delay]['vmaf_rows']
                ax.plot([r['timestamp_ms']/1000 for r in series], [r['vmaf'] for r in series],
                        color=COLORS[f'ideal{delay}'], lw=.7, ls=':' if delay == 0 else '--', label=f'Ideal {delay} ms')
            ax.set_ylim(0, 100)
            ax.legend(loc='upper left', ncol=2, frameon=False, fontsize=5.5, columnspacing=.8, handlelength=1.5)
        else:ax.set_ylim(bottom=0)
        ax.set_title(label, loc='left', fontsize=8, pad=3)
        ax.set_xlim(0, 20);ax.set_xticks([0, 5, 10, 15, 20])
        ax.tick_params(labelsize=7, length=2.5, width=.5, pad=2)
        ax.grid(axis='y', lw=.4, alpha=.2)
        if row == 2:ax.set_xlabel('Time (s)', fontsize=7, labelpad=2)
        for second in (5, 10, 15):ax.axvline(second, color='#999999', lw=.4, ls=':', zorder=0)
    bar_rows = []
    percentiles = [99, 95, 90, 50]
    bar_colors = {99: '#EE7733', 95: '#AA3377', 90: '#4477AA', 50: '#228833'}
    def bars(ax):
        delays = sorted(ideals)
        baseline = {r['timestamp_ms']: r['vmaf'] for r in ideals[0]['vmaf_rows']}
        for percentile in percentiles:
            values = []
            for delay in delays:
                current = {r['timestamp_ms']: r['vmaf'] for r in ideals[delay]['vmaf_rows']}
                assert set(current) == set(baseline)
                loss = [baseline[t]-current[t] for t in sorted(baseline)]
                value = float(np.percentile(loss, percentile, method='linear'))
                values.append(value)
                if len(bar_rows) < len(delays)*len(percentiles):
                    bar_rows.append({'delay_ms': delay, 'percentile': percentile, 'samples': len(loss), 'vmaf_loss': value})
            ax.bar(np.arange(len(delays)), values, width=.57, color=bar_colors[percentile], label=f'P{percentile}', zorder=3+percentiles.index(percentile))
        ax.set_xticks(np.arange(len(delays)), [str(d) for d in delays])
        ax.axhline(0, color='#888888', lw=.5);ax.grid(axis='y', lw=.4, alpha=.2, zorder=0)
        ax.legend(loc='upper left', ncol=4, frameon=False, fontsize=6, columnspacing=1, handlelength=1.2)
    bar = fig.add_axes([.085, .061, .875, .064])
    if ideals:
        bars(bar);bar.tick_params(labelsize=7, length=2.5, width=.5, pad=2)
        bar.set_title('VMAF loss (0 ms − delay); x: ideal one-way delay (ms)', loc='left', fontsize=8, pad=3)
    else:
        bar.set_axis_off();bar.text(0, .6, 'No 150 Mbps ideal run for this scene; ideal curves and loss bars omitted.', fontsize=7.5)
    common = len(qemu[0]['vmaf_rows'])
    black_ref = sum(r[2].getbbox() is None for r in references)
    black_qemu = sum(e['candidate_all_black'] is True for e in evidence if e['condition'] in CONTROLLERS)
    missing = sum(e['missing'] for e in evidence)
    fig.text(.055, .039, f'Images: repeat 1. Curves/bands: median/IQR over 5 repeats; CE/loss: sums over 5. Common VMAF: {common}/100.', fontsize=6.1)
    fig.text(.055, .027, f'Black views: reference {black_ref}/3, QEMU {black_qemu}/12. Missing shown snapshots: {missing}. Partial source runs: {len(partial)}/20.', fontsize=6.1)
    fig.text(.055, .015, '5 fps quality; 100 ms transport bins. Time zero: QEMU first object; ideal initial request. Missing samples remain gaps.', fontsize=6.1)
    for ext in ('pdf', 'png', 'svg'):fig.savefig(out/f'{scene}-scene-overview-a4.{ext}', dpi=220)
    plt.close(fig)
    if ideals:
        fig, ax = plt.subplots(figsize=(7.2, 3.8), layout='constrained');bars(ax)
        ax.set(xlabel='Ideal one-way delay (ms)', ylabel='VMAF loss (0 ms − delay)')
        for ext in ('pdf', 'png', 'svg'):fig.savefig(out/f'{scene}-vmaf-loss-percentiles.{ext}', dpi=180)
        plt.close(fig)
        with (out/'bar-percentiles.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(bar_rows[0]));writer.writeheader();writer.writerows(bar_rows)
    provenance = {'scene': scene, 'bandwidth_mbps': 150, 'qemu_base_rtt_ms': 20,
                  'repetitions_per_controller': 5, 'controllers': CONTROLLERS, 'ideal_delays_ms': sorted(ideals),
                  'qemu_common_vmaf_samples': common, 'partial_source_runs': partial, 'images': evidence,
                  'black_reference_images': black_ref, 'black_qemu_images': black_qemu,
                  'bar_definition': 'VMAF(0,t) - VMAF(delay,t); temporal P99/P95/P90/P50, linear percentiles, nested not summed',
                  'clock_alignment_limit': 'Guest/host clock skew not independently bounded; aggregate timing does not establish per-run causality',
                  'inputs_sha256': hashes, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out/'provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')
    print(f'Created {scene}: A4 overview, 20 QEMU runs, {common} common quality samples, ideal={bool(ideals)}', flush=True)


if __name__ == '__main__':main()
