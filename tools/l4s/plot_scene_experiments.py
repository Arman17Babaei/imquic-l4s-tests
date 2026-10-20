#!/usr/bin/env python3
"""Plot explicitly selected retained scene evidence; never launch experiments.

Manifest: {"cases": [{"scene": "playroom", "controller": "reno",
"downstream_mbps": 100, "directory": "/absolute/run",
"guest_directory": "/optional/explicit/guest"}]}. Relative paths resolve
against the manifest. Missing measurements remain missing and enter gap-report.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
import statistics
import textwrap


NOTES = [
    'Payload is browser object-envelope payload in nonoverlapping 100ms bins, including zero bins; not wire capacity.',
    'Native stream rates use per-connection counter deltas over actual monotonic sample intervals; stream bytes exclude retransmissions and packet overhead, so these are not wire capacity.',
    'First native samples, nonpositive intervals, missing counters, and decreased counters remain NaN; genuine zero deltas remain zero.',
    'Envelopes use browser receipt time within [trace start, nominal horizon); post-horizon envelopes are excluded.',
    'Envelope arrival and asynchronous payload acceptance differ; their byte reconciliation is reported, never silently corrected.',
    'Guest monotonic timestamps map through recorded guest wall-clock offset to browser trace-start Unix time. Cross-host clock and IPC skew are not bounded.',
    'Queue statistics are sampled snapshots, not packet residence distributions. No packet-residence CDF is inferred.',
    'SSIM uses retained clean-frame comparisons against the same complete-scene player at matched camera timestamps.',
    'Incomplete scenes at the fixed horizon are censored observations, not failed deliveries.',
]


def read_json(path):
    return json.loads(path.read_text())


def read_csv(path):
    if not path.is_file():
        return []
    with path.open(newline='') as stream:
        return list(csv.DictReader(stream))


def recorded_queue(state, role):
    """Describe saved qdisc evidence without inferring it from controller names."""
    marker = f'[{role}]'
    if marker not in state:
        return 'unrecorded'
    section = state.split(marker, 1)[1].split('\n[', 1)[0]
    for line in section.splitlines():
        if line.startswith('qdisc dualpi2 '):
            fields = {key: re.search(r'\b' + key + r' (\S+)', line)
                      for key in ('target', 'tupdate', 'step_thresh')}
            values = [fields[key][1] if fields[key] else 'unrecorded'
                      for key in ('target', 'tupdate', 'step_thresh')]
            return f'DualPI2 (target {values[0]} / update {values[1]} / step {values[2]})'
        if line.startswith('qdisc pfifo '):
            limit = re.search(r'\blimit (\d+)p\b', line)
            return f'FIFO {limit[1]} packets' if limit else 'FIFO (limit unrecorded)'
    return 'unrecorded'


def queue_labels(cases, field):
    values = {case['summary'].get(field, 'unrecorded') for case in cases}
    if len(values) == 1:
        return next(iter(values))
    return '; '.join(sorted({f"{case['summary']['controller']}/{case['summary']['downstream_mbps']}Mbps = "
                             + case['summary'].get(field, 'unrecorded') for case in cases}))


def recorded_htb_rate(state, role):
    marker = f'[{role}]'
    if marker not in state:
        return 'unrecorded'
    section = state.split(marker, 1)[1].split('\n[', 1)[0]
    match = re.search(r'\brate (\S+)', section)
    return match[1] if match else 'unrecorded'


def recorded_delay(state, role):
    marker = f'[{role}]'
    if marker not in state:
        return 'unrecorded'
    section = state.split(marker, 1)[1].split('\n[', 1)[0]
    if 'qdisc netem ' not in section:
        return 'none added'
    match = re.search(r'\bdelay (\S+)', section)
    return match[1] if match else 'unrecorded'


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def payload_bins(telemetry):
    trace = telemetry['trace']
    start, duration = trace['startedAtMs'], trace['nominalDurationMs']
    rows = [{'start_seconds': index / 10, 'end_seconds': min((index + 1) * 100, duration) / 1000,
             'payload_bytes': 0, 'objects': 0} for index in range(math.ceil(duration / 100))]
    for event in telemetry['events']:
        if event['type'] != 'object' or event.get('ignored') or event.get('accepted') is False:
            continue
        relative = event['timestampMs'] - start
        if 0 <= relative < duration:
            row = rows[min(int(relative // 100), len(rows) - 1)]
            row['payload_bytes'] += int(event['payload_bytes'])
            row['objects'] += 1
    for row in rows:
        row['payload_mbps'] = row['payload_bytes'] * 8 / ((row['end_seconds'] - row['start_seconds']) * 1e6)
    return rows


def zero_intervals(rows):
    intervals = []
    for row in rows:
        if row['payload_bytes']:
            continue
        if intervals and math.isclose(intervals[-1]['end_seconds'], row['start_seconds']):
            intervals[-1]['end_seconds'] = row['end_seconds']
        else:
            intervals.append({'start_seconds': row['start_seconds'], 'end_seconds': row['end_seconds']})
    for interval in intervals:
        interval['duration_seconds'] = round(interval['end_seconds'] - interval['start_seconds'], 9)
    return intervals


def guest_wall_offset_ms(samples):
    return statistics.median((sample['unix_ns'] - sample['monotonic_ns']) / 1e6 for sample in samples)


def relay_seconds(row, offset_ms, trace_start_unix_ms):
    return (int(row['monotonic_us']) / 1000 + offset_ms - trace_start_unix_ms) / 1000


def native_stream_rates(rows):
    """Retain raw counters and add rates without mixing connection histories."""
    previous = {}
    result = []
    for row in rows:
        current = dict(row)
        prior = previous.get(row['connection'])
        elapsed = ((int(row['monotonic_us']) - int(prior['monotonic_us'])) / 1e6
                   if prior is not None else math.nan)
        current['native_stream_interval_seconds'] = elapsed if elapsed > 0 else math.nan
        for counter, output in (('data_sent_bytes', 'native_stream_sent_mbps'),
                                ('data_received_bytes', 'native_stream_received_mbps')):
            rate = math.nan
            if elapsed > 0 and row.get(counter) not in (None, '') and prior.get(counter) not in (None, ''):
                delta = int(row[counter]) - int(prior[counter])
                if delta >= 0:
                    rate = delta * 8 / elapsed / 1e6
            current[output] = rate
        previous[row['connection']] = row
        result.append(current)
    return result


def resolve(base, value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def load_case(case, base):
    directory = resolve(base, case['directory'])
    telemetry = read_json(directory / 'player-telemetry.json')
    trace = telemetry['trace']
    duration = trace['nominalDurationMs'] / 1000
    if duration <= 0 or trace.get('startedUnixMs') is None:
        raise ValueError(f'missing or invalid trace clocks in {directory}')
    identifier = f"{case['scene']}-{case['controller']}-{case['downstream_mbps']}mbps"
    if not all(character.isalnum() or character in '-_.' for character in identifier):
        raise ValueError('scene/controller/rate must form a safe filename')
    gaps = []
    validation = read_json(directory / 'validation-summary.json') if (directory / 'validation-summary.json').is_file() else {}
    if validation.get('controller') not in (None, case['controller']):
        raise ValueError(f'manifest controller contradicts validation evidence: {directory}')
    guest = None
    if case.get('guest_directory'):
        guest = resolve(base, case['guest_directory'])
    elif (directory / 'guest-evidence.json').is_file():
        candidates = [resolve(base, path) for path in read_json(directory / 'guest-evidence.json')['directories']]
        candidates = [path for path in candidates if (path / 'network-samples.jsonl').is_file()]
        if len(candidates) == 1:
            guest = candidates[0]
        else:
            gaps.append(f'guest evidence missing or ambiguous: {len(candidates)} available directories')
    else:
        gaps.append('guest-evidence.json unavailable')
    if guest and (guest / 'provenance.json').is_file():
        configuration = read_json(guest / 'provenance.json').get('configuration', {})
        if 'rate_override' in configuration:
            recorded_rate = configuration['rate_override'] or '100mbit'
            if recorded_rate != f"{case['downstream_mbps']}mbit":
                raise ValueError(f'manifest rate contradicts guest evidence: {guest}')
    quality = read_csv(directory / 'quality/frame-quality.csv')
    quality = [dict(row, seconds=float(row['timestamp_ms']) / 1000,
                    scene_percent=100 * int(row['applied_gaussians']) / telemetry['sourceGaussians'])
               for row in quality if 0 <= float(row['timestamp_ms']) / 1000 <= duration]
    if not quality:
        gaps.append('no retained matched quality frames within horizon')
    samples = []
    if guest and (guest / 'network-samples.jsonl').is_file():
        samples = [json.loads(line) for line in (guest / 'network-samples.jsonl').read_text().splitlines() if line.strip()]
    offset = guest_wall_offset_ms(samples) if samples else None
    network = []
    selected_samples = []
    for sample in samples:
        seconds = (sample['unix_ns'] / 1e6 - trace['startedUnixMs']) / 1000
        if not 0 <= seconds < duration:
            continue
        selected_samples.append(sample)
        for role, device in sample['devices'].items():
            row = {'seconds': seconds, 'unix_ns': sample['unix_ns'], 'monotonic_ns': sample['monotonic_ns'],
                   'end_monotonic_ns': sample['end_monotonic_ns'], 'role': role, 'device': device['device'],
                   **device.get('qdisc', {})}
            stats = device['link'].get('stats64', device['link'].get('stats', {}))
            for direction in ('rx', 'tx'):
                for key in ('bytes', 'packets', 'dropped'):
                    row[f'{direction}_{key}'] = stats.get(direction, {}).get(key)
            network.append(row)
    if not network:
        gaps.append('no queue/link samples aligned within horizon')
    relay = []
    if guest and offset is not None:
        for row in read_csv(guest / 'transport-metrics.csv'):
            seconds = relay_seconds(row, offset, trace['startedUnixMs'])
            if 0 <= seconds < duration:
                relay.append(dict(row, seconds=seconds))
    if not relay:
        gaps.append('no relay transport samples aligned within horizon')
    relay = native_stream_rates(relay)
    bins = payload_bins(telemetry)
    pretrace_bytes = sum(int(event.get('payload_bytes', 0)) for event in telemetry['events']
                         if event['type'] == 'object' and event['timestampMs'] < trace['startedAtMs'])
    envelope_bytes = sum(row['payload_bytes'] for row in bins)
    accepted = trace.get('payloadBytesAtHorizon')
    if accepted is not None and accepted != envelope_bytes + pretrace_bytes:
        gaps.append('accepted horizon payload differs from received envelope sum; async boundary/duplicates cannot be reconstructed exactly')
    intervals = [(second['monotonic_ns'] - first['monotonic_ns']) / 1e6
                 for first, second in zip(selected_samples, selected_samples[1:])]
    summary = {'case': identifier, 'scene': case['scene'], 'controller': case['controller'],
               'downstream_mbps': case['downstream_mbps'], 'duration_seconds': duration,
               'directory': str(directory), 'guest_directory': str(guest) if guest else None,
               'envelope_payload_bytes_in_window': envelope_bytes, 'pretrace_envelope_payload_bytes': pretrace_bytes,
               'accepted_payload_bytes_at_horizon': accepted,
               'acceptance_minus_envelopes_bytes': accepted - envelope_bytes - pretrace_bytes if accepted is not None else None,
               'mean_envelope_payload_mbps': envelope_bytes * 8 / duration / 1e6,
               'zero_payload_bins': sum(row['payload_bytes'] == 0 for row in bins),
               'longest_zero_payload_interval_seconds': max((row['duration_seconds'] for row in zero_intervals(bins)), default=0),
               'source_gaussians': telemetry['sourceGaussians'], 'applied_gaussians_at_horizon': trace.get('gaussiansAtHorizon'),
               'received_gaussians_at_horizon': trace.get('receivedGaussiansAtHorizon'),
               'applied_scene_percent_at_horizon': 100 * trace.get('gaussiansAtHorizon', 0) / telemetry['sourceGaussians'],
               'censored_partial_scene': trace.get('gaussiansAtHorizon', 0) < telemetry['sourceGaussians'],
               'mean_sampled_ssim': statistics.mean(float(row['rgb_ssim']) for row in quality) if quality else None,
               'quality_frames': len(quality), 'relay_samples': len(relay), 'network_samples': len(selected_samples),
               'guest_wall_minus_monotonic_ms': offset,
               'median_network_interval_ms': statistics.median(intervals) if intervals else None,
               'max_network_interval_ms': max(intervals) if intervals else None}
    summary['validation_status'] = validation.get('status')
    state_path = guest / 'tc-state-before.txt' if guest else None
    state = state_path.read_text() if state_path and state_path.is_file() else ''
    summary['provider_queue'] = recorded_queue(state, 'provider_egress')
    summary['downstream_queue'] = recorded_queue(state, 'downstream_egress')
    summary['provider_rate'] = recorded_htb_rate(state, 'provider_egress')
    endpoints = ('server_egress', 'client_egress', 'background_sink_egress',
                 'background_source_egress', 'background_sink2_egress')
    summary['endpoint_propagation'] = ', '.join(sorted({recorded_delay(state, role) for role in endpoints}))
    summary['rendering_status'] = validation.get('rendering', {}).get('status')
    if validation.get('status') != 'PASS':
        gaps.append('run validation PASS unavailable; retained measurements do not imply experiment acceptance')
    if intervals and statistics.median(intervals) > 150:
        gaps.append('actual median queue sampling interval exceeds the nominal 100ms target; see measured cadence')
    for field, scale, name in (('rtt_us', 1000, 'median_relay_rtt_ms'), ('cwnd_bytes', 1024, 'median_relay_cwnd_kib')):
        values = [float(row[field]) / scale for row in relay if row.get(field) not in (None, '')]
        summary[name] = statistics.median(values) if values else None
    for field in ('packets_lost', 'ce_packets', 'ect0_packets', 'ect1_packets'):
        deltas = []
        for connection in sorted({row['connection'] for row in relay}):
            values = [int(row[field]) for row in relay if row['connection'] == connection and row.get(field) not in (None, '')]
            if len(values) >= 2:
                deltas.append(values[-1] - values[0])
        summary[f'relay_{field}_sample_delta'] = sum(deltas) if deltas else None
    for role in ('provider_egress', 'downstream_egress'):
        for field in ('backlog_bytes', 'delay_c_us', 'delay_l_us'):
            values = [row[field] for row in network if row['role'] == role and row.get(field) is not None]
            summary[f'{role}_max_{field}'] = max(values) if values else None
    if samples:
        offsets = [(sample['unix_ns'] - sample['monotonic_ns']) / 1e6 for sample in samples]
        summary['guest_clock_offset_range_ms'] = max(offsets) - min(offsets)
    return {'summary': summary, 'bins': bins, 'quality': quality, 'relay': relay, 'network': network,
            'zero_intervals': zero_intervals(bins), 'gaps': gaps}


def draw_group(scene, rate, cases, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, grid = plt.subplots(4, 2, figsize=(13, 11), sharex=True)
    axes = list(grid.flat)
    labels = ['Application / native stream (Mbps)', 'Matched RGB SSIM', 'Applied scene (%)', 'Relay RTT (ms)',
              'Relay cwnd (KiB)', 'Relay cumulative lost / CE packets', 'Sampled queue backlog (KiB)', 'Sampled queue delay (ms)']
    for case in cases:
        summary = case['summary']
        controller = summary['controller']
        color = {'reno': '#b94b43', 'prague': '#177c9b'}[controller]
        bins, quality = case['bins'], case['quality']
        axes[0].stairs([row['payload_mbps'] for row in bins], [row['start_seconds'] for row in bins] + [bins[-1]['end_seconds']], label=f'{controller} app (100ms)', color=color, linewidth=1)
        for axis, field in ((axes[1], 'rgb_ssim'), (axes[2], 'scene_percent')):
            axis.plot([row['seconds'] for row in quality], [float(row[field]) for row in quality], label=controller, color=color)
        connections = sorted({row['connection'] for row in case['relay']})
        for connection in connections:
            relay = [row for row in case['relay'] if row['connection'] == connection]
            suffix = f' {connection}' if len(connections) > 1 else ''
            axes[0].plot([row['seconds'] for row in relay], [row['native_stream_sent_mbps'] for row in relay],
                         color=color, linestyle='--', linewidth=1.3, label=f'{controller}{suffix} native send')
            for axis, field, scale in ((axes[3], 'rtt_us', 1000), (axes[4], 'cwnd_bytes', 1024)):
                axis.plot([row['seconds'] for row in relay], [float(row[field]) / scale for row in relay], label=controller + suffix, color=color)
            for field, style in (('packets_lost', '-'), ('ce_packets', '--')):
                if relay and field in relay[0]:
                    baseline = int(relay[0][field])
                    axes[5].plot([row['seconds'] for row in relay], [int(row[field]) - baseline for row in relay], linestyle=style,
                                 label=f'{controller}{suffix} {field}', color=color)
        for role, style in (('provider_egress', '-'), ('downstream_egress', '--')):
            rows = [row for row in case['network'] if row['role'] == role]
            def values(field, scale):
                return [float(row[field]) / scale if row.get(field) is not None else math.nan for row in rows]
            axes[6].plot([row['seconds'] for row in rows], values('backlog_bytes', 1024), color=color, linestyle=style,
                         label=f'{controller} {role.split("_")[0]}')
            for field, delay_style in (('delay_c_us', style), ('delay_l_us', ':')):
                if any(field in row for row in rows):
                    axes[7].plot([row['seconds'] for row in rows], values(field, 1000), color=color, linestyle=delay_style,
                                 label=f'{controller} {role.split("_")[0]} {field[6]}')
    for axis, label in zip(axes, labels):
        axis.set_ylabel(label, fontsize=9)
        axis.grid(alpha=.2)
        axis.set_xlim(0, max(case['summary']['duration_seconds'] for case in cases))
        handles, legend_labels = axis.get_legend_handles_labels()
        if handles:
            axis.legend(handles, legend_labels, fontsize=6, loc='upper right')
    for axis in grid[-1]: axis.set_xlabel('Trace time (s)')
    axes[1].set_ylim(0, 1)
    peak_percent = max((row['scene_percent'] for case in cases for row in case['quality']), default=0)
    axes[2].set_ylim(0, min(100, max(1, peak_percent * 1.1)))
    present = {case['summary']['controller'] for case in cases}
    missing = {'reno', 'prague'} - present
    fig.suptitle(f'{scene} — downstream {rate} Mbit/s' + (f' | single controller: {next(iter(present))}' if missing else ''), fontsize=14)
    recorded = (f"Recorded queues: s1 {queue_labels(cases, 'provider_queue')}; "
                f"s2 {queue_labels(cases, 'downstream_queue')}; endpoint propagation {queue_labels(cases, 'endpoint_propagation')}")
    provider_rate = queue_labels(cases, 'provider_rate')
    strip = (f'Media: server 10.0.0.1 → s1 [HTB {provider_rate} + DualPI2] → s2 [HTB {rate} Mbit/s] → client 10.0.0.2\n'
             f'{textwrap.fill(recorded, 145)}\n'
             'Background: server → s1 → s2 → bg_sink; bg_src → s1 → s2 → bg_sink2; two unlimited non-ECN Cubic flows\n'
             'Solid: app envelope payload / 100ms; dashed: native stream-send / actual interval; neither measures wire capacity. No packet residence inference.')
    fig.text(.5, .023, strip, ha='center', va='bottom', fontsize=8)
    fig.tight_layout(rect=(0, .115, 1, .96))
    for extension in ('png', 'pdf'):
        fig.savefig(output / f'{scene}-{rate}mbps.{extension}', dpi=160)
    plt.close(fig)


def rate_comparison_groups(cases):
    """Pair measured rates only within the same scene and transport controller."""
    groups = {}
    for case in cases:
        summary = case['summary']
        groups.setdefault((summary['scene'], summary['controller']), []).append(case)
    return [(scene, controller, sorted(selected, key=lambda case: case['summary']['downstream_mbps']))
            for (scene, controller), selected in sorted(groups.items())
            if {case['summary']['downstream_mbps'] for case in selected} == {20, 100}]


def draw_rates(scene, controller, cases, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, grid = plt.subplots(2, 2, figsize=(12, 7.5), sharex=True)
    axes = list(grid.flat)
    for case in cases:
        rate = case['summary']['downstream_mbps']
        label = f'{rate} Mbps'
        color = {20: '#ad6800', 100: '#3158a8'}[rate]
        bins = case['bins']
        axes[0].stairs([row['payload_mbps'] for row in bins],
                       [row['start_seconds'] for row in bins] + [bins[-1]['end_seconds']],
                       label=f'{label} app (100ms)', color=color, linewidth=1)
        quality = case['quality']
        for axis, field in ((axes[1], 'rgb_ssim'), (axes[2], 'scene_percent')):
            axis.plot([row['seconds'] for row in quality], [float(row[field]) for row in quality],
                      label=label, color=color)
        connections = sorted({row['connection'] for row in case['relay']})
        for connection in connections:
            rows = [row for row in case['relay'] if row['connection'] == connection]
            suffix = f' {connection}' if len(connections) > 1 else ''
            axes[0].plot([row['seconds'] for row in rows], [row['native_stream_sent_mbps'] for row in rows],
                         color=color, linestyle='--', linewidth=1.3, label=f'{label}{suffix} native send')
            axes[3].plot([row['seconds'] for row in rows], [float(row['rtt_us']) / 1000 for row in rows],
                         color=color, label=label + (f' {connection}' if len(connections) > 1 else ''))
    labels = ['Application / native stream (Mbps)', 'Matched RGB SSIM', 'Applied scene (%)', 'Relay RTT (ms)']
    for axis, label in zip(axes, labels):
        axis.set_ylabel(label)
        axis.set_xlim(0, max(case['summary']['duration_seconds'] for case in cases))
        axis.grid(alpha=.2)
        handles, legend_labels = axis.get_legend_handles_labels()
        if handles:
            axis.legend(handles, legend_labels, fontsize=8)
    for axis in grid[-1]:
        axis.set_xlabel('Trace time (s)')
    axes[1].set_ylim(0, 1)
    peak_percent = max((row['scene_percent'] for case in cases for row in case['quality']), default=0)
    axes[2].set_ylim(0, min(100, max(1, peak_percent * 1.1)))
    fig.suptitle(f'{scene} — {controller.title()} — downstream rate comparison', fontsize=14)
    aqm = queue_labels(cases, 'downstream_queue')
    recorded = (f"Recorded queues: s1 {queue_labels(cases, 'provider_queue')}; s2 {aqm}; "
                f"downstream HTB burst/cburst 128k; endpoint propagation {queue_labels(cases, 'endpoint_propagation')}")
    strip = (f"Media: server → s1 [{queue_labels(cases, 'provider_rate')} + DualPI2] → s2 [20 or 100 Mbps] → client\n"
             f'{textwrap.fill(recorded, 135)}\n'
             'Two unlimited non-ECN Cubic flows: server → bg_sink; bg_src → bg_sink2, both via s1 → s2\n'
             'Solid: app envelope payload / 100ms; dashed: native stream-send / actual interval; neither measures wire capacity. Partial scenes are censored.')
    fig.text(.5, .018, strip, ha='center', va='bottom', fontsize=8)
    fig.tight_layout(rect=(0, .145, 1, .95))
    for extension in ('png', 'pdf'):
        fig.savefig(output / f'{scene}-{controller}-rates.{extension}', dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = read_json(args.manifest)
    args.output.mkdir(parents=True, exist_ok=True)
    cases = []
    seen = set()
    for definition in manifest['cases']:
        if definition['controller'] not in ('reno', 'prague') or definition['downstream_mbps'] not in (20, 100):
            raise ValueError('controller must be reno/prague; downstream_mbps must be 20/100')
        case = load_case(definition, args.manifest.resolve().parent)
        identifier = case['summary']['case']
        if identifier in seen:
            raise ValueError(f'duplicate case: {identifier}')
        seen.add(identifier)
        cases.append(case)
        for name, field in (('payload-100ms', 'bins'), ('quality', 'quality'), ('relay', 'relay'), ('network', 'network')):
            write_csv(args.output / f'{identifier}-{name}.csv', case[field])
    summaries = [case['summary'] for case in cases]
    write_csv(args.output / 'summary.csv', summaries)
    (args.output / 'summary.json').write_text(json.dumps({'cases': summaries, 'notes': NOTES}, indent=2) + '\n')
    gaps = {'notes': NOTES, 'cases': [{'case': case['summary']['case'], 'data_gaps': case['gaps'],
                                     'zero_payload_intervals': case['zero_intervals']} for case in cases]}
    groups = sorted({(case['summary']['scene'], case['summary']['downstream_mbps']) for case in cases})
    gaps['missing_comparisons'] = []
    for scene, rate in groups:
        selected = [case for case in cases if case['summary']['scene'] == scene and case['summary']['downstream_mbps'] == rate]
        missing = {'reno', 'prague'} - {case['summary']['controller'] for case in selected}
        if missing:
            gaps['missing_comparisons'].append({'scene': scene, 'downstream_mbps': rate, 'controllers': sorted(missing)})
        draw_group(scene, rate, selected, args.output)
    for scene, controller, selected in rate_comparison_groups(cases):
        draw_rates(scene, controller, selected, args.output)
    (args.output / 'gap-report.json').write_text(json.dumps(gaps, indent=2) + '\n')
    print(json.dumps({'cases': len(cases), 'output': str(args.output.resolve()), 'missing_comparisons': gaps['missing_comparisons']}))


if __name__ == '__main__': main()
