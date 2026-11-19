#!/usr/bin/env python3
"""Offline retained-evidence analysis for configurable experiment suites.

No experiment, browser, or VM is started here. VMAF motion is evaluated on the
exact common sparse PNG sequence, never interpolated or filled from other runs.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess


LIMITATIONS = [
    'Same-player full-scene references measure progressive completeness, not independent renderer accuracy.',
    'VMAF is exploratory for sparse progressive rendering; its motion features use adjacent common PNG samples.',
    'Missing timestamps are excluded without interpolation.',
    'Repeat ranges are descriptive, not confidence intervals.',
    'Browser object payload goodput excludes protocol/link overhead and is not wire utilization or link capacity.',
    'Payload bins prefer receivedAtMs receipt time; legacy timestampMs fallback measures processing completion and may shift under worker backlog.',
]


def write_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def write_csv(path: Path, rows: list[dict], fields=None):
    fields = fields or list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def safe_child(root: Path, relative: str):
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f'path outside batch: {relative}')
    return result


def checksum(path: Path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run_command(argv, log: Path, commands: list, *, backend=None, data=None):
    args = [str(arg) for arg in argv]
    record = {'argv': args, 'log': str(log), 'requested_backend': backend}
    commands.append(record)
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write_bytes(result.stdout)
    record['exit_code'] = result.returncode
    if result.returncode:
        raise RuntimeError(f'command exited {result.returncode}; see {log.name}')
    if backend == 'cuda' and re.search(
            r'using\s+cpu|fall(?:ing)?\s*back|fallback|cuda[^\n]*(?:unavailable|disabled|not supported)',
            result.stdout.decode(errors='replace'), re.I):
        raise RuntimeError(f'CUDA fallback rejected; see {log.name}')
    return result.stdout


def common_poses(series: list[dict]):
    if not series:
        return []
    common = set(series[0]).intersection(*(set(item) for item in series[1:]))
    return [timestamp for timestamp in sorted(common)
            if all(item[timestamp][0]['cameraPose'] == series[0][timestamp][0]['cameraPose']
                   for item in series[1:])]


def frame_pairs(directory: Path):
    manifests = []
    for kind in ('candidate', 'reference'):
        manifest_path = directory / f'{kind}-frames/quality-frames.json'
        if not manifest_path.is_file():
            manifest_path = directory / f'{kind}-frames/frames.json'
        manifest = read_json(manifest_path, {})
        frames = {}
        for frame in manifest.get('frames', []):
            timestamp = frame.get('timestampMs')
            if number(timestamp) is None or not isinstance(frame.get('cameraPose'), dict):
                raise ValueError('frame timestamp/cameraPose missing')
            if timestamp in frames:
                raise ValueError('duplicate frame timestamp')
            image = safe_child(manifest_path.parent, frame['file'])
            if not image.is_file():
                continue
            frames[timestamp] = dict(frame, _path=image)
        manifests.append((manifest, frames))
    candidates, references = manifests[0][1], manifests[1][1]
    pairs = {timestamp: (candidate, references[timestamp])
             for timestamp, candidate in candidates.items()
             if timestamp in references and candidate['cameraPose'] == references[timestamp]['cameraPose']}
    metadata = {'candidate_frames': len(candidates), 'reference_frames': len(references),
                'exact_reference_pairs': len(pairs),
                'unpaired_candidate_timestamps_ms': sorted(set(candidates) - set(pairs)),
                'requested_fps': manifests[0][0].get('requestedFps'),
                'camera_timestamps': manifests[0][0].get('cameraTimestamps'),
                'candidate_capture_complete': manifests[0][0].get('complete'),
                'reference_capture_complete': manifests[1][0].get('complete')}
    return pairs, metadata


def missing_requested_timestamps(common: list, fps: float, count: int):
    # Tolerance is numeric roundoff (one nanosecond), never a capture-slot tolerance.
    return [slot * 1000 / fps for slot in range(count)
            if not any(math.isclose(timestamp, slot * 1000 / fps, rel_tol=0, abs_tol=1e-6)
                       for timestamp in common)]


def browser_bins(telemetry: dict, duration: float):
    trace = telemetry.get('trace', {})
    start = number(trace.get('startedAtMs'))
    if start is None or duration <= 0:
        return []
    offline = telemetry.get('deliveryModel') == 'offline-imquic-v1'
    rows = [{'seconds': i, 'duration_seconds': min(1.0, duration - i), 'payload_bytes': 0,
             'modeled_arrival_events': 0,
             'receipt_timestamp_events': 0, 'legacy_completion_timestamp_events': 0}
            for i in range(math.ceil(duration))]
    for event in telemetry.get('events', []):
        receipt = number(event.get('receivedAtMs'))
        timestamp = number(event.get('ideal_arrival_ms')) if offline else receipt if receipt is not None else number(event.get('timestampMs'))
        payload = number(event.get('payload_bytes'))
        if event.get('type') == 'object' and timestamp is not None and payload is not None:
            seconds = (timestamp - start) / 1000
            if 0 <= seconds < duration:
                rows[int(seconds)]['payload_bytes'] += payload
                field = 'modeled_arrival_events' if offline else 'receipt_timestamp_events' if receipt is not None else 'legacy_completion_timestamp_events'
                rows[int(seconds)][field] += 1
    for row in rows:
        row['browser_payload_mbps'] = row['payload_bytes'] * 8 / row['duration_seconds'] / 1e6
    return rows


def load_run(batch: Path, item: dict, index: int, out: Path):
    run = {key: item.get(key) for key in (
        'id', 'scene', 'bandwidth_mbps', 'delay_ms', 'duration_seconds', 'path', 'controller',
        'switches', 'rerun', 'experiment_index', 'selected_attempt', 'background', 'background_cc', 'background_warmup_seconds', 'background_link_delay_ms', 'qemu_delay_mode',
        'background_profile','dualpi2_target_ms','dualpi2_update_ms','dualpi2_step_ms','dualpi2_coupling_factor','delivery_model')}
    if run['path'] == 'ideal' and run['delivery_model'] is None:
        run['delivery_model'] = 'live-ideal-legacy'
    run.update(status='BLOCKED', quality_frames=0, mean_ssim=None, mean_browser_payload_mbps=None,
               capture_status=None, quality_status=None)
    quality, bins, pairs = [], [], {}
    selected = item.get('selected_attempt')
    attempt = next((value for value in item.get('attempts', [])
                    if selected is not None and value['index'] == selected), None)
    if attempt is None:
        run['error'] = 'no explicitly selected attempt'
        return run, quality, bins, pairs
    for key in ('capture_status', 'quality_status', 'process_exit_code', 'criterion_pass'):
        run[key] = attempt.get(key)
    run['source_attempt_status'] = attempt.get('status')
    try:
        directory = safe_child(batch, attempt['directory'])
        run['source_validation'] = read_json(directory / 'validation-summary.json', {})
        run['source_validation_status'] = run['source_validation'].get('status')
        run['directory'] = str(directory.relative_to(batch.resolve()))
        quality_path = directory / 'quality/frame-quality.csv'
        if quality_path.is_file():
            with quality_path.open() as stream:
                for raw in csv.DictReader(stream):
                    timestamp = number(raw.get('timestamp_ms'))
                    if timestamp is not None and 0 <= timestamp <= float(item['duration_seconds']) * 1000:
                        quality.append({key: number(value) for key, value in raw.items()})
        telemetry = read_json(directory / 'player-telemetry.json', {})
        total = number(telemetry.get('sourceGaussians'))
        for row in quality:
            row['applied_fraction'] = (row.get('applied_gaussians') / total
                                       if total and row.get('applied_gaussians') is not None else None)
        bins = browser_bins(telemetry, float(item['duration_seconds']))
        receipt_count = sum(row['receipt_timestamp_events'] for row in bins)
        legacy_count = sum(row['legacy_completion_timestamp_events'] for row in bins)
        modeled_count = sum(row['modeled_arrival_events'] for row in bins)
        run['browser_payload_timestamp_basis'] = (
            'ideal_arrival_ms simulated payload arrival' if telemetry.get('deliveryModel') == 'offline-imquic-v1' else
            'receivedAtMs with legacy timestampMs completion fallback' if receipt_count and legacy_count else
            'receivedAtMs browser receipt' if receipt_count else
            'legacy timestampMs processing completion fallback' if legacy_count else 'no timed object events')
        run['browser_payload_receipt_events'] = receipt_count
        run['browser_payload_legacy_completion_events'] = legacy_count
        run['browser_payload_modeled_arrival_events'] = modeled_count
        run['quality_frames'] = len(quality)
        values = [row['rgb_ssim'] for row in quality if row.get('rgb_ssim') is not None]
        run['mean_ssim'] = statistics.mean(values) if values else None
        comparison = read_json(directory / 'quality/comparison.json', {})
        run['snapshot_coverage'] = comparison.get('snapshot_coverage', {})
        run['snapshot_loss_accepted'] = run['snapshot_coverage'].get('accepted') is True
        run['max_missing_fraction'] = run['snapshot_coverage'].get('max_missing_fraction')
        pairs, metadata = frame_pairs(directory)
        run.update(metadata)
        run['quality_frames'] = len(quality)
        values = [row['rgb_ssim'] for row in quality if row.get('rgb_ssim') is not None]
        run['mean_ssim'] = statistics.mean(values) if values else None
        run['mean_browser_payload_mbps'] = (sum(row['payload_bytes'] for row in bins) * 8 /
                                            float(item['duration_seconds']) / 1e6 if bins else None)
        run['status'] = ('PASS' if quality and all(run.get(key) == 'PASS' for key in
                         ('capture_status', 'quality_status')) and
                         run['source_attempt_status'] == 'PASS' and
                         run.get('process_exit_code') in (None, 0) and
                         run.get('criterion_pass') is not False and
                         run['source_validation_status'] in (None, 'PASS') else 'PARTIAL')
        write_csv(out / f'run-{index:04d}-quality.csv', quality,
                  ['frame', 'timestamp_ms', 'rgb_ssim', 'psnr_db', 'applied_gaussians', 'applied_fraction'])
        write_csv(out / f'run-{index:04d}-browser-payload.csv', bins,
                  ['seconds', 'duration_seconds', 'payload_bytes', 'browser_payload_mbps',
                   'receipt_timestamp_events', 'legacy_completion_timestamp_events', 'modeled_arrival_events'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        run['error'] = str(error)
        run['status'] = 'PARTIAL' if quality else 'BLOCKED'
    return run, quality, bins, pairs


def vmaf_group(out: Path, name: str, members: list, common: list, settings: dict):
    """Retain raw scores, commands, checksums and logs, including failed evaluations."""
    result = {'vmaf_status': 'BLOCKED', 'vmaf_backend': settings.get('vmaf_backend', 'cuda')}
    work = out / name
    work.mkdir(exist_ok=True)
    commands, hashes, scores = [], {}, {}
    backend = result['vmaf_backend']
    try:
        if backend not in ('cuda', 'cpu'):
            raise ValueError('vmaf_backend must be cuda or cpu')
        binary = Path(settings.get('vmaf_binary') or '/missing-vmaf')
        model = Path(settings.get('vmaf_model') or '/missing-vmaf-model')
        if not binary.is_absolute() or not model.is_absolute() or not binary.is_file() or not model.is_file():
            raise ValueError('VMAF binary/model must be existing absolute paths')
        for path in (binary, model):
            hashes[str(path)] = checksum(path)
        result.update(binary=str(binary), model=str(model), binary_sha256=hashes[str(binary)],
                      model_sha256=hashes[str(model)], threads=0,
                      backend_evidence='explicit gpumask request, binary hash and retained logs; no kernel profiler')
        # Preserve available build configuration without invoking a build or profiler.
        for path in (binary.parent.parent / 'meson-info/intro-buildoptions.json',
                     binary.parent.parent / 'meson-private/cmd_line.txt'):
            if path.is_file():
                hashes[str(path)] = checksum(path)
                (work / path.name).write_bytes(path.read_bytes())
                if path.name == 'intro-buildoptions.json':
                    result['build_cuda_enabled'] = next((option.get('value') for option in read_json(path)
                                                         if option.get('name') == 'enable_cuda'), None)
        if backend == 'cuda' and result.get('build_cuda_enabled') is False:
            raise ValueError('recorded VMAF build has CUDA disabled; refusing CUDA request')
        if not common:
            raise ValueError('no exact common timestamp/camera poses with per-run references')
        from PIL import Image
        dimensions = None
        reference_equal = True
        for timestamp in common:
            reference_rgb = None
            for _, _, _, pairs in members:
                candidate, reference = pairs[timestamp]
                for frame in (candidate, reference):
                    path = frame['_path']
                    hashes[str(path)] = checksum(path)
                    with Image.open(path) as image:
                        if image.mode not in ('RGB', 'RGBA'):
                            raise ValueError('VMAF requires 8-bit RGB PNG inputs')
                        if image.mode == 'RGBA' and image.getchannel('A').getextrema() != (255, 255):
                            raise ValueError('transparent RGBA input has no defined reference composite')
                        if dimensions is None:
                            dimensions = image.size
                        if image.size != dimensions:
                            raise ValueError('native dimensions differ across compared frames')
                        if frame is reference:
                            rgb = image.convert('RGB').tobytes()
                            if reference_rgb is not None and reference_rgb != rgb:
                                reference_equal = False
                            reference_rgb = rgb
        result['rgb_references_equal'] = reference_equal
        width, height = dimensions
        if width % 2 or height % 2:
            raise ValueError('native dimensions must be even for YUV420p')
        result['native_dimensions'] = [width, height]
        for index, (run, _, _, pairs) in enumerate(members):
            paths = {}
            for kind, frame_index in (('candidate', 0), ('reference', 1)):
                target = work / f'run-{index}-{kind}.yuv'
                paths[kind] = target
                run_command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'image2pipe',
                             '-framerate', '1', '-vcodec', 'png', '-i', 'pipe:0', '-vf',
                             'scale=in_range=full:out_range=limited:out_color_matrix=bt709,format=yuv420p',
                             '-fps_mode', 'passthrough', '-f', 'rawvideo', target],
                            work / f'convert-{index}-{kind}.log', commands,
                            data=b''.join(pairs[timestamp][frame_index]['_path'].read_bytes() for timestamp in common))
                if target.stat().st_size != len(common) * width * height * 3 // 2:
                    raise ValueError('YUV frame count/dimensions mismatch')
                hashes[str(target)] = checksum(target)
            raw = work / f'run-{index}-vmaf.json'
            run_command([binary, '--reference', paths['reference'], '--distorted', paths['candidate'],
                         '--width', width, '--height', height, '--pixel_format', '420', '--bitdepth', '8',
                         '--model', f'path={model}', '--threads', '0', '--json', '--gpumask',
                         '0' if backend == 'cuda' else '-1', '--output', raw],
                        work / f'run-{index}-vmaf.log', commands, backend=backend)
            frames = read_json(raw)['frames']
            values = [number(frame['metrics']['vmaf']) for frame in frames]
            if len(values) != len(common) or any(value is None for value in values):
                raise ValueError('VMAF returned missing/nonfinite frame scores')
            rows = [{'timestamp_ms': timestamp, 'vmaf': value}
                    for timestamp, value in zip(common, values)]
            write_csv(work / f'run-{index}-vmaf.csv', rows)
            run['mean_vmaf'] = statistics.mean(values)
            run['vmaf_frames'] = len(values)
            run['vmaf_rows'] = rows
            scores[str(run['id'])] = {'mean': statistics.mean(values), 'minimum': min(values), 'maximum': max(values)}
        result.update(vmaf_status='PASS', scores=scores)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError) as error:
        result['vmaf_error'] = str(error)
    finally:
        write_json(work / 'commands.json', commands)
        write_json(work / 'input-checksums.json', hashes)
        # Converted inputs are reproducible from retained PNGs; retain hashes, not bulky raw sequences.
        for target in work.glob('*.yuv'):
            target.unlink()
    return result


def evaluate_available_vmaf(out: Path, name: str, members: list, common: list, settings: dict):
    """Keep every selected run visible; missing evidence cannot suppress usable runs."""
    for run, _, _, pairs in members:
        run.update(vmaf_status='PENDING' if pairs else 'MISSING_EVIDENCE',
                   vmaf_timestamps_ms=common if common else sorted(pairs),
                   vmaf_grid='shared exact poses' if common else 'per-run exact poses')
        if not pairs:
            run['vmaf_error'] = 'no exact candidate/reference pairs in selected attempt'
    # Shared sequences preserve comparability when all runs have usable evidence.
    if common:
        shared = vmaf_group(out, name, members, common, settings)
        if shared['vmaf_status'] == 'PASS':
            for run, *_ in members:
                run['vmaf_status'] = 'PASS'
            shared['vmaf_means_comparable'] = bool(shared.get('rgb_references_equal'))
            return shared
        shared_error = shared.get('vmaf_error')
    else:
        shared_error = 'no exact shared timestamp/camera grid across all selected attempts'
    evaluations = {}
    for index, member in enumerate(members):
        run, _, _, pairs = member
        if not pairs:
            continue
        grid = common or sorted(pairs)
        run['vmaf_timestamps_ms'] = grid
        evaluated = vmaf_group(out, f'{name}-run-{index:04d}', [member], grid, settings)
        run['vmaf_status'] = evaluated['vmaf_status']
        if evaluated.get('vmaf_error'):
            run['vmaf_error'] = evaluated['vmaf_error']
        evaluations[str(run['id'])] = evaluated
    passed = sum(run['vmaf_status'] == 'PASS' for run, *_ in members)
    return {'vmaf_status': 'PARTIAL' if passed else 'BLOCKED',
            'vmaf_error': shared_error, 'vmaf_means_comparable': False,
            'vmaf_grid': 'per-run exact poses; means are not comparable across runs',
            'per_run_vmaf': evaluations}


def matching_references(left: dict, right: dict, timestamps: list):
    """Compare decoded RGB references; filenames/PNG encodings need not match."""
    from PIL import Image
    matched = []
    for timestamp in timestamps:
        try:
            with Image.open(left[timestamp][1]['_path']) as a, Image.open(right[timestamp][1]['_path']) as b:
                if a.size == b.size and a.convert('RGB').tobytes() == b.convert('RGB').tobytes():
                    matched.append(timestamp)
        except (OSError, KeyError, ValueError):
            continue
    return matched


def quality_differences(members: list):
    """Signed within-repeat pairs, including explicit unavailable planned members."""
    differences = []
    delays = sorted({member[0]['delay_ms'] for member in members}, reverse=True)
    if members[0][0]['path'] == 'qemu':
        conditions = [('prague', 'reno', 'Prague − Reno')]
        if any(member[0]['controller'] == 'reno-ect0' for member in members):
            conditions += [('prague', 'reno-ect0', 'Prague − Reno ECT(0)'),
                           ('reno-ect0', 'reno', 'Reno ECT(0) − Reno')]
    else:
        conditions = [(high, low, f'{high:g} ms − {low:g} ms')
                      for index, high in enumerate(delays) for low in delays[index + 1:]]
    field = 'controller' if members[0][0]['path'] == 'qemu' else 'delay_ms'
    for rerun in sorted({member[0]['rerun'] for member in members}):
        for high, low, label in conditions:
            candidates = [[member for member in members
                           if member[0]['rerun'] == rerun and member[0][field] == value]
                          for value in (high, low)]
            pair = {'label': label, 'rerun': rerun, 'rows': []}
            for role, options in zip(('minuend', 'subtrahend'), candidates):
                pair[role + '_id'] = options[0][0]['id'] if len(options) == 1 else None
                pair[role + '_status'] = options[0][0]['status'] if len(options) == 1 else 'MISSING_OR_AMBIGUOUS'
            for metric in ('ssim', 'vmaf'):
                pair[metric] = {'status': 'UNAVAILABLE', 'reason': 'missing or ambiguous member in this repeat',
                                'count': 0, 'mean': None}
            if all(len(options) == 1 for options in candidates):
                left, right = (options[0] for options in candidates)
                common = common_poses([left[3], right[3]])
                try:
                    aligned = matching_references(left[3], right[3], common)
                except ImportError:
                    aligned = []
                for metric, source, value_key in (('ssim', 1, 'rgb_ssim'), ('vmaf', 0, 'vmaf')):
                    rows = [member[source] if source else member[0].get('vmaf_rows', [])
                            for member in (left, right)]
                    values = [{row['timestamp_ms']: row.get(value_key) for row in sequence}
                              for sequence in rows]
                    usable = [timestamp for timestamp in aligned
                              if all(number(value.get(timestamp)) is not None for value in values)]
                    reason = 'no exact timestamp/pose pairs with equal decoded RGB references and quality scores'
                    if metric == 'vmaf':
                        grids = [member[0].get('vmaf_timestamps_ms', []) for member in (left, right)]
                        if not (grids[0] and grids[0] == grids[1] and
                                len(set(grids[0])) == len(grids[0]) and
                                grids[0] == usable and
                                all(member[0].get('vmaf_status') == 'PASS' for member in (left, right))):
                            usable = []
                            reason = 'VMAF requires identical full temporal grids, poses, RGB references and successful scores'
                    delta = [values[0][timestamp] - values[1][timestamp] for timestamp in usable]
                    pair[metric] = {'status': 'AVAILABLE' if delta else 'UNAVAILABLE',
                                    'reason': None if delta else reason, 'count': len(delta),
                                    'mean': statistics.mean(delta) if delta else None}
                    for timestamp, value in zip(usable, delta):
                        row = next((row for row in pair['rows'] if row['timestamp_ms'] == timestamp), None)
                        if row is None:
                            row = {'timestamp_ms': timestamp}
                            pair['rows'].append(row)
                        row['delta_' + metric] = value
                pair['rows'].sort(key=lambda row: row['timestamp_ms'])
            differences.append(pair)
    return differences


def plot_group(out: Path, name: str, group: dict, members: list):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(6, 1, figsize=(11, 15), sharex=True)
    for run, quality, bins, _ in members:
        label = f"{run['controller']} {run['delay_ms']} ms, repeat {run['rerun']} [{run['status']}; quality {run['quality_status']}]"
        for axis, rows, xkey, ykey, factor in (
                (axes[0], run.get('vmaf_rows', []), 'timestamp_ms', 'vmaf', 0.001),
                (axes[1], quality, 'timestamp_ms', 'rgb_ssim', 0.001),
                (axes[2], bins, 'seconds', 'browser_payload_mbps', 1),
                (axes[3], quality, 'timestamp_ms', 'applied_fraction', 0.001)):
            values = [row for row in rows if row.get(ykey) is not None and row.get(xkey) is not None]
            if values:
                axis.plot([row[xkey] * factor for row in values], [row[ykey] for row in values],
                          marker='.', linewidth=0.7, linestyle='-' if ykey == 'browser_payload_mbps' else 'none',
                          label=label)
    for axis, metric in zip(axes[4:], ('vmaf', 'ssim')):
        for pair in group['differences']:
            values = [row for row in pair['rows'] if row.get('delta_' + metric) is not None]
            label = f"{pair['label']}, repeat {pair['rerun']} [{pair['minuend_status']}/{pair['subtrahend_status']}]"
            if values:
                axis.plot([row['timestamp_ms'] / 1000 for row in values],
                          [row['delta_' + metric] for row in values], marker='.', linestyle='none', label=label)
        if not axis.lines:
            reasons = list(dict.fromkeys(pair[metric]['reason'] for pair in group['differences']))
            import textwrap
            axis.text(.5, .5, '\n'.join(textwrap.fill(reason, 100) for reason in reasons if reason)
                      or 'No comparison pair planned', transform=axis.transAxes, ha='center', fontsize=8)
    for axis, title in zip(axes, ('VMAF', 'RGB SSIM', 'Browser payload (Mbps)', 'Applied scene fraction',
                                  'ΔVMAF', 'ΔSSIM')):
        axis.set_ylabel(title)
        axis.grid(alpha=0.25)
        if axis.lines:
            axis.legend(fontsize=7)
        elif axis not in axes[4:]:
            axis.text(0.5, 0.5, 'No retained usable data', transform=axis.transAxes, ha='center')
    for axis in axes[4:]:
        axis.axhline(0, color='black', linewidth=.7, alpha=.5)
    first = members[0][0]
    background_label=(f"{first['switches']} TCP {first.get('background_cc')} backgrounds, each targeting {first['bandwidth_mbps']} Mbps"
                      if first.get('background') else 'no background traffic')
    if first.get('background_profile') in ('cubic-prague-unlimited','bbr-prague-unlimited'):
        classic_cc='BBR' if first['background_profile']=='bbr-prague-unlimited' else 'Cubic'
        background_label=f'Unlimited TCP {classic_cc} + Prague, shared inter-switch DualPI2'
        background_label += (f"; target {first.get('dualpi2_target_ms')} ms, update {first.get('dualpi2_update_ms')} ms, "
                             f"step {first.get('dualpi2_step_ms')} ms, coupling {first.get('dualpi2_coupling_factor')}")
    if first.get('background') and first.get('background_link_delay_ms'):
        background_label += f", +{first['background_link_delay_ms']:g} ms per dedicated link each direction"
    delay_unit = 'ms/link' if first.get('qemu_delay_mode')=='per-link' and first['path']=='qemu' else 'ms'
    delay_label = ', '.join(f'{delay:g}' for delay in sorted({run['delay_ms'] for run, *_ in members}, reverse=True))
    model_label = (f"ideal ({first.get('delivery_model', 'live-ideal-legacy')})"
                   if first['path'] == 'ideal' else first['path'])
    axes[0].set_title(f"{first['scene']}, {first['bandwidth_mbps']} Mbps, {model_label}, {delay_label} {delay_unit}, "
                      f"{first['switches']} switches, {first['duration_seconds']} s\n"
                      f"{background_label}\n"
                      f"{group['common_pose_count']} exact shared poses; analysis {group['status']}; "
                      f"{len(members)} runs; " + ', '.join(sorted({run['status'] for run, *_ in members})))
    axes[-1].set_xlabel('Camera timestamp (s); payload: simulated arrivals'
        if first.get('delivery_model') == 'offline-imquic-v1' else
        'Camera timestamp (s); payload: receivedAtMs, legacy timestampMs fallback')
    axes[-1].set_xlim(0, float(first['duration_seconds']))
    fig.tight_layout()
    for suffix in ('png', 'pdf'):
        fig.savefig(out / f'{name}.{suffix}', dpi=170)
    plt.close(fig)


def analyze_batch(batch: Path, ledger: dict, settings: dict) -> dict:
    """Analyze only ledger-selected attempts and write exclusively under analysis/."""
    batch = Path(batch).resolve()
    if not settings.get('enabled', True):
        return {'status': 'DISABLED', 'runs': [], 'groups': []}
    out = batch / 'analysis'
    if out.is_symlink():
        raise ValueError('analysis output directory cannot be a symlink')
    out.mkdir(parents=True, exist_ok=True)
    if any(path.is_symlink() for path in out.rglob('*')):
        raise ValueError('analysis output must not contain symlinks')
    records = [load_run(batch, item, index, out) for index, item in enumerate(ledger.get('items', []))]
    grouped = {}
    for record in records:
        run = record[0]
        key_values = [run.get(field) for field in (
            'scene', 'bandwidth_mbps', 'duration_seconds', 'path', 'switches', 'experiment_index',
            'background', 'background_cc', 'background_warmup_seconds', 'background_link_delay_ms', 'qemu_delay_mode', 'delivery_model')]
        if run['path'] == 'qemu':
            key_values.extend(run.get(field) for field in ('delay_ms','background_profile',
                'dualpi2_target_ms','dualpi2_update_ms','dualpi2_step_ms','dualpi2_coupling_factor'))
        key = json.dumps(key_values, sort_keys=True)
        grouped.setdefault(key, []).append(record)
    groups = []
    for index, (key, members) in enumerate(grouped.items()):
        members.sort(key=lambda member: (-member[0]['delay_ms'], member[0]['rerun'],
                                         0 if member[0]['controller'] == 'prague' else 1))
        name = f'group-{index:04d}'
        selected_members = [member for member in members if member[0]['selected_attempt'] is not None]
        common = common_poses([member[3] for member in selected_members])
        group = {'id': name, 'key': json.loads(key), 'runs': [value[0]['id'] for value in members],
                 'common_pose_count': len(common), 'common_timestamps_ms': common,
                 'vmaf_status': 'DISABLED', 'status': 'PASS'}
        for run, _, _, pairs in members:
            run['common_pose_count'] = len(common) if run['selected_attempt'] is not None else 0
            run['common_fraction_of_candidate_frames'] = (len(common) / run['candidate_frames']
                                                          if run.get('candidate_frames') else None)
            run['excluded_candidate_timestamps_ms'] = sorted(set(pairs) - set(common))
            fps = number(run.get('requested_fps'))
            expected = math.ceil(float(run['duration_seconds']) * fps - 1e-9) if fps and fps > 0 else None
            run['expected_quality_samples'] = expected
            run['common_coverage'] = len(common) / expected if expected else None
            if fps and fps > 0 and run.get('camera_timestamps') == 'fixed':
                run['missing_requested_timestamps_ms'] = missing_requested_timestamps(common, fps, expected)
        if settings.get('vmaf', False):
            group.update(evaluate_available_vmaf(out, name, selected_members, common, settings))
        group['differences'] = quality_differences(members)
        difference_rows = []
        for pair in group['differences']:
            metadata = {key: value for key, value in pair.items() if key not in ('rows', 'ssim', 'vmaf')}
            for metric in ('ssim', 'vmaf'):
                metadata.update({f'{metric}_{key}': value for key, value in pair[metric].items()})
            difference_rows.extend(dict(metadata, **row) for row in (pair['rows'] or [{}]))
        write_csv(out / f'{name}-differences.csv', difference_rows)
        repeat_conditions = {}
        for run, *_ in members:
            condition = f"{run['controller']}:{run['delay_ms']}ms"
            repeat_conditions.setdefault(condition, []).append(run)
        group['repeats'] = {}
        for condition, runs in repeat_conditions.items():
            detail = {'planned_count': len(runs), 'selected_count': sum(run['selected_attempt'] is not None for run in runs)}
            for metric in ('mean_ssim', 'mean_vmaf', 'mean_browser_payload_mbps'):
                comparable = metric != 'mean_vmaf' or group.get('vmaf_means_comparable', False)
                values = [run[metric] for run in runs if run.get(metric) is not None]
                detail[metric] = {'count': len(values), 'mean': statistics.mean(values) if values and comparable else None,
                                  'comparable': comparable,
                                  'descriptive_min': min(values) if values else None,
                                  'descriptive_max': max(values) if values else None}
            group['repeats'][condition] = detail
        if any(run['status'] != 'PASS' for run, *_ in members) or group['vmaf_status'] in ('BLOCKED', 'PARTIAL'):
            group['status'] = 'PARTIAL' if any(run['quality_frames'] for run, *_ in members) else 'BLOCKED'
        if settings.get('vmaf') and any((run.get('missing_requested_timestamps_ms') and
                not run.get('snapshot_loss_accepted')) or
                not run.get('reference_capture_complete') for run, *_ in members):
            group['status'] = 'PARTIAL' if any(run['quality_frames'] or run.get('vmaf_frames') for run, *_ in members) else 'BLOCKED'
        if settings.get('plots', True):
            try:
                plot_group(out, name, group, members)
                group['plots_status'] = 'PASS'
            except (ImportError, OSError, ValueError, RuntimeError) as error:
                group.update(plots_status='BLOCKED', plots_error=str(error), status='PARTIAL')
        groups.append(group)
    runs = [record[0] for record in records]
    status = ('PASS' if groups and all(group['status'] == 'PASS' for group in groups)
              else 'PARTIAL' if any(run['quality_frames'] for run in runs) else 'BLOCKED')
    summary = {'status': status, 'runs': runs, 'groups': groups, 'limitations': LIMITATIONS}
    write_json(out / 'summary.json', summary)
    scalar_rows = [{key: value for key, value in run.items()
                    if not isinstance(value, (dict, list))} for run in runs]
    write_csv(out / 'summary.csv', scalar_rows)
    lines = ['# Offline experiment-suite analysis', '', f'Analysis status: **{status}**.', '',
             'Only explicitly selected attempts are included. Capture and quality statuses remain separate.', '',
             '| Run | Capture | Quality | SSIM frames | Shared poses |',
             '|---|---|---|---:|---:|']
    lines.extend(f"| {run['id']} | {run['capture_status']} | {run['quality_status']} | "
                 f"{run['quality_frames']} | {run.get('common_pose_count', 0)} |" for run in runs)
    lines += ['', '[Machine-readable summary](summary.json) and [run table](summary.csv).', '']
    for group in groups:
        if group['differences']:
            lines += [f"[{group['id']} signed quality differences]({group['id']}-differences.csv).", '']
            for pair in group['differences']:
                lines += [f"{pair['label']}, repeat {pair['rerun']}: " + '; '.join(
                    f"{metric.upper()} {pair[metric]['status']} (n={pair[metric]['count']}, "
                    f"mean={pair[metric]['mean']})" +
                    (f"; {pair[metric]['reason']}" if pair[metric]['reason'] else '')
                    for metric in ('vmaf', 'ssim')), '']
        if group.get('plots_status') == 'PASS':
            lines += [f"![{group['id']}]({group['id']}.png)", f"[PDF]({group['id']}.pdf)", '']
        if group.get('vmaf_error'):
            lines += [f"{group['id']} VMAF BLOCKED: {group['vmaf_error']}", '']
    lines += ['## Interpretation limits', ''] + ['- ' + item for item in LIMITATIONS]
    (out / 'README.md').write_text('\n'.join(lines) + '\n')
    return summary
