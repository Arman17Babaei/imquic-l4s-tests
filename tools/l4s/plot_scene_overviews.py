"""A4 Bicycle overview from retained, exact-pose images and computed metrics."""
from pathlib import Path
import argparse
import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
PARSER = argparse.ArgumentParser(description=__doc__)
PARSER.add_argument('--scene', required=True)
PARSER.add_argument('--output', type=Path, required=True)
PARSER.add_argument('--bbr-transport', type=Path)
PARSER.add_argument('--bbr-source', type=Path)
ARGS = PARSER.parse_args()
SCENE = ARGS.scene
DISPLAY = {'drjohnson': 'Dr Johnson', 'treehill': 'Treehill'}.get(SCENE, SCENE.title())
OUT = ARGS.output.resolve()
OUT.mkdir(parents=True, exist_ok=True)
IDEAL = ROOT / 'results/player-experiments-20261117-010729'
QEMU = ROOT / 'results/imquic-qemu-50mbps-rtt30ms-q5-queue16-20261117-131136/batch'
TRANSPORT = OUT / 'transport'
SCORES = ROOT / f'results/ideal-vmaf-percentiles-50mbps-20261117/{SCENE}/scores.json'
TIMES = (5000, 10000, 15000)
COLORS = {'prague': '#007C91', 'reno': '#B34F2B', 'ideal0': '#555555', 'ideal50': '#8D5AA5', 'bbr': '#AD8300'}
INPUTS = {}


def read(path):
    raw = path.read_bytes()
    INPUTS[str(path.resolve())] = hashlib.sha256(raw).hexdigest()
    return raw


def jread(path):
    return json.loads(read(path))


def selected(batch, controller, delay):
    ledger = jread(batch / 'ledger.json')
    matches = [i for i in ledger['items'] if i['scene'] == SCENE
               and i['bandwidth_mbps'] == 50 and i['controller'] == controller
               and i['delay_ms'] == delay and i['rerun'] == 1]
    assert len(matches) == 1
    item = matches[0]
    attempt = next(a for a in item['attempts'] if a['index'] == item['selected_attempt'])
    assert attempt['quality_status'] == 'PASS'
    assert attempt['status'] == 'PASS' or (controller == 'bbr' and attempt['status'] == 'PARTIAL' and (batch/'recovery.json').is_file())
    assert item['duration_seconds'] == 20
    return item, batch / attempt['directory']


def frame_at(directory, timestamp):
    manifest = jread(directory / 'frames.json')
    frames = [f for f in manifest['frames'] if f['timestampMs'] == timestamp]
    assert len(frames) == 1, (directory, timestamp)
    frame = frames[0]
    path = directory / frame['file']
    read(path)
    image = Image.open(path).convert('RGB')
    assert image.size == (960, 540)
    return frame, path, image


def main():
    conditions = [
        ('ideal0', 'Ideal\n0 ms one-way', *selected(IDEAL, 'ideal', 0)),
        ('ideal50', 'Ideal\n50 ms one-way', *selected(IDEAL, 'ideal', 50)),
        ('prague', 'Prague\n30 ms RTT', *selected(QEMU, 'prague', 15)),
        ('reno', 'Reno\n30 ms RTT', *selected(QEMU, 'reno', 15)),
    ]
    if ARGS.bbr_source:
        assert SCENE == 'bicycle' and ARGS.bbr_transport
        conditions.append(('bbr', 'BBR (1 run)\n30 ms RTT', *selected(ARGS.bbr_source.resolve(), 'bbr', 15)))
    references = [frame_at(conditions[0][3] / 'reference-frames', t) for t in TIMES]
    pictures = [('reference', 'Full reference', [r[2] for r in references])]
    evidence = []
    for key, label, item, directory in conditions:
        images = []
        for index, timestamp in enumerate(TIMES):
            candidate, path, image = frame_at(directory / 'candidate-frames', timestamp)
            reference, reference_path, reference_image = frame_at(directory / 'reference-frames', timestamp)
            assert candidate['cameraPose'] == reference['cameraPose'] == references[index][0]['cameraPose']
            assert reference_image.tobytes() == references[index][2].tobytes()
            assert reference['appliedGaussians'] == references[index][0]['appliedGaussians']
            images.append(image)
            evidence.append({'condition': key, 'id': item['id'], 'repeat': 1,
                             'timestamp_ms': timestamp, 'candidate': str(path.resolve()),
                             'reference': str(reference_path.resolve()),
                             'camera_pose_equal': True, 'reference_rgb_equal': True,
                             'candidate_all_black': image.getbbox() is None})
        pictures.append((key, label, images))

    stats = list(csv.DictReader(read(TRANSPORT / 'plotted-data.csv').decode().splitlines()))
    if ARGS.bbr_transport:
        stats += list(csv.DictReader(read(ARGS.bbr_transport.resolve() / 'plotted-data.csv').decode().splitlines()))
    scores = jread(SCORES)
    for condition in conditions[:2]:
        assert any(i['id'] == condition[2]['id'] for i in scores['key']['inputs'])
    # Validate the cached score input hashes before reusing the existing scores.
    for cached in scores['key']['inputs']:
        assert cached['capture_status'] == cached['quality_status'] == 'PASS'
        for kind in ('candidate', 'reference'):
            manifest = Path(cached['attempt_directory']) / f'{kind}-frames/frames.json'
            assert hashlib.sha256(read(manifest)).hexdigest() == cached[f'{kind}_manifest_sha256']

    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 7.5,
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.linewidth': .6, 'pdf.fonttype': 42, 'svg.fonttype': 'none'})
    width, height = 210 / 25.4, 297 / 25.4
    fig = plt.figure(figsize=(width, height), facecolor='white')
    fig.text(.055, .975, DISPLAY, fontsize=13, weight='bold', va='top')
    fig.text(.055, .952, '50 Mbps  |  Ideal: 0 / 50 ms one-way  |  QEMU: 30 ms base RTT, Cubic + Prague backgrounds',
             fontsize=7.5, va='top', color='#444444')

    image_left, image_right = (.215, .935) if ARGS.bbr_source else (.200, .955)
    gap = .105 if ARGS.bbr_source else .070
    image_width = (image_right - image_left - 2 * gap) / 3
    image_height = image_width * width / height * 9 / 16
    image_top = .918
    row_stride = image_height + .008
    for col, timestamp in enumerate(TIMES):
        fig.text(image_left + col * (image_width + gap) + image_width / 2,
                 .928, f'{timestamp / 1000:g} s', ha='center', fontsize=9)
    for row, (key, label, images) in enumerate(pictures):
        bottom = image_top - row * row_stride - image_height
        fig.text(.055, bottom + image_height / 2, label, va='center', fontsize=8,
                 color=COLORS.get(key, '#222222'))
        for col, image in enumerate(images):
            ax = fig.add_axes([image_left + col * (image_width + gap), bottom, image_width, image_height])
            ax.imshow(image, interpolation='none')
            ax.set_axis_off()

    metrics = [('vmaf', 'VMAF'), ('goodput', 'QUIC stream throughput (Mbps)'),
               ('rtt', 'Smoothed RTT (ms)'), ('cwnd', 'cwnd (KiB)'),
               ('ce', 'Sum CE (packets / 100 ms)'), ('loss', 'Sum loss (packets / 100 ms)')]
    centers = (np.arange(200) + .5) / 10
    axes = []
    for index, (metric, label) in enumerate(metrics):
        row, col = divmod(index, 2)
        ax = fig.add_axes([.085 + col * .49, .387 - row * .107, .385, .065])
        axes.append(ax)
        for controller in (('prague', 'reno', 'bbr') if ARGS.bbr_source else ('prague', 'reno')):
            rows = [r for r in stats if r['metric'] == metric and r['controller'] == controller]
            times = np.array([float(r['time_s']) for r in rows])
            if metric in ('ce', 'loss'):
                values = np.array([float(r['sum']) for r in rows])
            else:
                values = np.array([float(r['median']) if r['median'] else np.nan for r in rows])
                low = np.array([float(r['q25']) if r['q25'] else np.nan for r in rows])
                high = np.array([float(r['q75']) if r['q75'] else np.nan for r in rows])
                if controller != 'bbr':
                    ax.fill_between(times, low, high, color=COLORS[controller], alpha=.16, lw=0)
            ax.plot(times, values, lw=.65, color=COLORS[controller], label='BBR (1 run)' if controller == 'bbr' else controller.title(), ls='--' if controller == 'bbr' else '-')
        if metric == 'vmaf':
            for delay, key in [(0, 'ideal0'), (50, 'ideal50')]:
                series = scores['scores'][str(delay)]
                ax.plot([r['timestamp_ms'] / 1000 for r in series], [r['vmaf'] for r in series],
                        color=COLORS[key], lw=.7, ls='--' if delay == 50 else ':', label=f'Ideal {delay} ms')
            ax.set_ylim(0, 100)
            ax.legend(loc='upper left', bbox_to_anchor=(-.015, 1.05), ncol=2,
                      frameon=False, fontsize=6, columnspacing=.9, handlelength=1.5)
        else:
            ax.set_ylim(bottom=0)
        ax.set_title(label, loc='left', fontsize=8, pad=3)
        ax.set_xlim(0, 20)
        ax.set_xticks([0, 5, 10, 15, 20])
        ax.tick_params(labelsize=7, length=2.5, width=.5, pad=2)
        ax.grid(axis='y', lw=.4, alpha=.2)
        if row == 2:
            ax.set_xlabel('Time (s)', fontsize=7, labelpad=2)
        for second in (5, 10, 15):
            ax.axvline(second, color='#999999', lw=.4, ls=':', zorder=0)

    # Loss is baseline minus delayed VMAF, so degradation has positive sign.
    delays = [0, 1, 5, 10, 50, 100, 500]
    percentiles = [99, 95, 90, 50]
    bar_colors = {99: '#EE7733', 95: '#AA3377', 90: '#4477AA', 50: '#228833'}
    baseline = scores['scores']['0']
    bar_rows = []
    bar = fig.add_axes([.085, .061, .875, .064])
    for percentile in percentiles:
        values = []
        for delay in delays:
            current = scores['scores'][str(delay)]
            assert len(current) == len(baseline) == 200
            assert [r['timestamp_ms'] for r in current] == [r['timestamp_ms'] for r in baseline]
            loss = np.array([b['vmaf'] - r['vmaf'] for r, b in zip(current, baseline)])
            value = float(np.percentile(loss, percentile, method='linear'))
            values.append(value)
            bar_rows.append({'delay_ms': delay, 'percentile': percentile, 'samples': 200, 'vmaf_loss': value})
        bar.bar(np.arange(len(delays)), values, width=.57, color=bar_colors[percentile],
                label=f'P{percentile:02d}', zorder=3 + percentiles.index(percentile))
    bar.set_xticks(np.arange(len(delays)), [str(d) for d in delays])
    bar.tick_params(labelsize=7, length=2.5, width=.5, pad=2)
    bar.axhline(0, color='#888888', lw=.5)
    bar.grid(axis='y', lw=.4, alpha=.2, zorder=0)
    bar.set_title('VMAF loss (0 ms − delay); x: ideal one-way delay (ms)', loc='left', fontsize=8, pad=3)
    bar.legend(loc='upper left', ncol=4, frameon=False, fontsize=6,
               columnspacing=1, handlelength=1.2)
    with (OUT / 'bar-percentiles.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(bar_rows[0]))
        writer.writeheader()
        writer.writerows(bar_rows)

    fig.text(.055, .039, ('Images: repeat 1. Prague/Reno: median/IQR and CE/loss sums over five repeats. BBR: one run.' if ARGS.bbr_source else 'Images: QEMU repeat 1. Curves/bands: QEMU median/IQR over five repeats; CE/loss: sums over five repeats.'),
             fontsize=6.5, color='#444444')
    black_references = sum(r[2].getbbox() is None for r in references)
    black_count = sum(i['candidate_all_black'] for i in evidence if i['condition'] in ('prague','reno'))
    fig.text(.055, .016, f'Black views shown: reference {black_references}/3, Prague/Reno {black_count}/6. ' +
             ('BBR: one sample; quality recovered after launcher OOM.' if ARGS.bbr_source else 'Preserved as recorded; not missing captures.'),
             fontsize=6.5, color='#444444')
    fig.text(.055, .027, 'Ideal curves: one deterministic run per delay. Time zero: ideal initial request; QEMU first applied object. Reference poses match.',
             fontsize=6.5, color='#444444')
    for ext in ('pdf', 'png', 'svg'):
        fig.savefig(OUT / f'{SCENE}-scene-overview-a4.{ext}', dpi=220)
    plt.close(fig)
    # Separate metric PDF for the paper; LaTeX owns the main-text panel layout.
    fig, ax = plt.subplots(figsize=(7.2, 3.8), layout='constrained')
    for percentile in percentiles:
        values = [next(r['vmaf_loss'] for r in bar_rows
                       if r['delay_ms'] == delay and r['percentile'] == percentile) for delay in delays]
        ax.bar(np.arange(len(delays)), values, width=.57, color=bar_colors[percentile],
               label=f'P{percentile:02d}', zorder=3 + percentiles.index(percentile))
    ax.set_xticks(np.arange(len(delays)), [str(d) for d in delays])
    ax.set(xlabel='Ideal one-way delay (ms)', ylabel='VMAF loss (0 ms − delay)')
    ax.axhline(0, color='#888888', lw=.6)
    ax.grid(axis='y', alpha=.2, lw=.5, zorder=0)
    ax.legend(loc='upper left', ncol=4, frameon=False, fontsize=8)
    for ext in ('pdf', 'png', 'svg'):
        fig.savefig(OUT / f'{SCENE}-vmaf-loss-percentiles.{ext}', dpi=180)
    plt.close(fig)
    provenance = {'black_reference_snapshots': black_references, 'black_prague_reno_snapshots': black_count, 'bbr_sample_count': int(bool(ARGS.bbr_source)), 'scene': SCENE, 'page_mm': [210, 297], 'pages': 1, 'bandwidth_mbps': 50,
                  'bar_chart': {'metric': 'VMAF loss', 'delays_ms': delays, 'percentiles': percentiles,
                                'definition': 'VMAF(0,t) - VMAF(delay,t)',
                                'aggregation': 'temporal percentiles over 200 paired samples, numpy linear; nested bars, not summed'},
                  'image_timestamps_ms': list(TIMES), 'qemu_image_repeat': 1,
                  'qemu_plot_repeats_per_controller': {'prague':5,'reno':5,**({'bbr':1} if ARGS.bbr_source else {})}, 'ideal_one_way_delays_ms': [0, 50],
                  'qemu_base_rtt_ms': 30, 'qemu_modes': ['prague', 'reno'] + (['bbr'] if ARGS.bbr_source else []),
                  'time_origins': {'ideal': 'initial request', 'qemu': 'first applied object'},
                  'bbr_recovery':jread(ARGS.bbr_source.resolve()/'recovery.json') if ARGS.bbr_source and (ARGS.bbr_source.resolve()/'recovery.json').exists() else None,
                  'images': evidence, 'inputs_sha256': INPUTS,
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (OUT / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    (OUT / 'README.md').write_text('''# Scene A4 overview

One A4 portrait page with exact 5, 10 and 15 second full-reference and candidate frames, followed by six metric panels. An additional nested VMAF loss bar chart shows baseline VMAF minus delayed VMAF: positive values mean degradation. P99, P95, P90 and P50 of loss correspond to the negated P01, P05, P10 and P50 of the signed delay-minus-baseline differences. The chart uses 200 paired timestamps for each ideal delay; bars overlap, rather than being summed. Packet loss retains its original line plot. All images retain their complete original 16:9 field of view. QEMU pictures are repeat 1, chosen in advance; plots retain all five repeats. Ideal 0 and 50 ms curves use retained 10 fps VMAF scores; QEMU VMAF is 5 fps with original missing samples preserved. Transport panels use 100 ms bins, median/IQR for throughput, RTT and cwnd, and per-bin sums across five repeats for CE and loss.

The four conditions share exactly matching camera poses and decoded reference pixels at all three image timestamps. Ideal propagation is 0 or 50 ms one-way; QEMU propagation remains 30 ms base RTT. QEMU Reno is Not-ECT. No Reno ECT(0) measurements have been added. Recorded black candidate and reference images are preserved and counted in the footer and provenance; they are not missing images. Some retained full references are black or blurred, limiting perceptual interpretation even at high VMAF. Any BBR curve is one diagnostic sample, not repeated-run evidence. Ideal has no equivalent cwnd/RTT/ECN series and is shown only in VMAF and images. Time zero is initial request for ideal and first applied object for QEMU. Native host/guest clock skew is not independently bounded; aggregate event timing does not establish per-run causality.

Reproduce using tools/l4s/plot_scene_transport.py then tools/l4s/plot_scene_overviews.py with --scene and --output; see the parent manifest for exact commands. This reads retained data and cached scores only, and launches no experiment or quality job. Source hashes and individual frame paths are in provenance.json. See the sibling Bicycle QEMU template for raw and per-run transport exports.
''')
    print(f'Created {SCENE}: one A4 page; all {len(evidence)} candidate poses and reference RGB comparisons verified.')


if __name__ == '__main__':
    main()
