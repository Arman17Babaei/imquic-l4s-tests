#!/usr/bin/env python3
"""Classify saved player delivery gaps without launching a browser or network.

PASS means no sustained gap was found in the recorded observation, not proof
that a controller or harness is optimal. REVIEW requires inspection; congestion
and application scheduling evidence remain visible rather than being waived.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import sys

COUNTERS = ('data_sent_bytes', 'data_received_bytes', 'cwnd_bytes', 'bytes_in_flight',
            'queued_stream_bytes', 'picoquic_queued_bytes', 'sendable_bytes',
            'connection_credit_bytes', 'ready_streams', 'flow_blocked_streams',
            'pacing_Bps', 'rtt_us', 'packets_sent', 'packets_lost', 'timer_losses',
            'spurious_losses', 'max_reorder_delay_us', 'max_reorder_gap',
            'output_queue_bytes', 'output_queue_full_us')


def counter_evidence(rows):
    result = {'samples': len(rows)}
    if rows:
        result['bounds_monotonic_us'] = [rows[0]['monotonic_us'], rows[-1]['monotonic_us']]
    for key in COUNTERS:
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if values:
            result[key] = {'first': values[0], 'last': values[-1],
                           'delta': values[-1] - values[0], 'min': min(values), 'max': max(values)}
    ages = [r['monotonic_us'] - r['send_state_time_us'] for r in rows
            if r.get('send_state_time_us', 0) > 0]
    if ages:
        result['send_state_age_us_max'] = max(ages)
    return result


def bracket(rows, lo, hi):
    """One sample outside either boundary exposes the counter measurement bounds."""
    inside = [r for r in rows if lo <= r['monotonic_us'] <= hi]
    before = [r for r in rows if r['monotonic_us'] < lo]
    after = [r for r in rows if r['monotonic_us'] > hi]
    return (before[-1:] + inside + after[:1])


def clock_alignment(telemetry, network_samples):
    trace = telemetry['trace']
    anchors = [e['monotonic_us'] / 1000 - e['timestampMs']
               for e in telemetry.get('events', [])
               if isinstance(e.get('monotonic_us'), (int, float)) and 'timestampMs' in e]
    origins = [s['unix_ns'] / 1e6 - s['monotonic_ns'] / 1e6 for s in network_samples
               if 'unix_ns' in s and 'monotonic_ns' in s]
    if origins and isinstance(trace.get('startedUnixMs'), (int, float)):
        offset = trace['startedUnixMs'] - trace['startedAtMs'] - statistics.median(origins)
        # Wall clocks are not guaranteed synchronized. A negative observed
        # transit delay bounds clock disagreement; positive delay includes queueing.
        uncertainty = max(1, max(origins) - min(origins), max(anchors, default=offset) - offset)
        method = 'guest_wall_monotonic_and_browser_start_wall'
    elif anchors:
        # Minimum observed transit delay is unknown, so this is an estimate only.
        offset = max(anchors)
        uncertainty = max(1, max(anchors) - min(anchors))
        method = 'fastest_control_or_object_anchor_unknown_minimum_transit_delay'
    else:
        raise ValueError('no guest wall-clock mapping or native/browser timestamp anchors')
    return {'method': method, 'guest_minus_browser_ms': offset,
            'observed_uncertainty_ms': uncertainty,
            'uncertainty_is_bound': False,
            'note': 'Host/guest wall skew and minimum transit latency are not independently calibrated.',
            'anchor_offset_range_ms': [min(anchors), max(anchors)] if anchors else None,
            'guest_wall_origin_spread_ms': max(origins) - min(origins) if origins else None}


def gaps(points, start, end):
    selected = sorted(set([start, end] + [p for p in points if start <= p <= end]))
    return [(a, b) for a, b in zip(selected, selected[1:]) if b > a]


def analyze(telemetry, *, relay_rows, network_samples=(), gap_seconds=1):
    if not math.isfinite(gap_seconds) or gap_seconds <= 0:
        raise ValueError('gap threshold must be positive and finite')
    trace = telemetry.get('trace', {})
    start, duration = trace.get('startedAtMs'), trace.get('nominalDurationMs')
    source = telemetry.get('sourceGaussians')
    if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)) or duration <= 0:
        raise ValueError('telemetry needs startedAtMs and positive nominalDurationMs')
    if not isinstance(source, int) or source <= 0:
        raise ValueError('telemetry needs positive sourceGaussians')
    end = start + duration
    threshold = gap_seconds * 1000
    objects = sorted((e for e in telemetry.get('events', []) if e.get('type') == 'object'),
                     key=lambda e: e['timestampMs'])
    eligible = [e for e in objects if start <= e['timestampMs'] <= end]
    horizon_counters = 'receivedGaussiansAtHorizon' in trace and 'gaussiansAtHorizon' in trace
    if horizon_counters:
        full = trace['receivedGaussiansAtHorizon'] == source and trace['gaussiansAtHorizon'] == source
    else:
        # Older draining runs can have complete final counters despite an
        # incomplete observation. Later objects make completion time ambiguous.
        full = (telemetry.get('receivedGaussians') == source and telemetry.get('appliedGaussians') == source
                and not any(e['timestampMs'] > end for e in objects))
    # Full-transfer counters prove completion; the last in-window object is a
    # conservative proxy for its time. Post-horizon events never extend progress.
    before_end = [e for e in objects if e['timestampMs'] <= end]
    complete_at = max(start, before_end[-1]['timestampMs']) if full and before_end else None
    if full and not before_end:
        raise ValueError('complete transfer has no object timestamp evidence')
    pending_end = complete_at if complete_at is not None else end
    clock = clock_alignment(telemetry, network_samples)
    offset = clock['guest_minus_browser_ms']
    native_start, native_end = (start + offset) * 1000, (pending_end + offset) * 1000
    relay = sorted(relay_rows, key=lambda r: r['monotonic_us'])
    subscriber = sorted((e for e in telemetry.get('events', []) if e.get('type') == 'transport-metrics'),
                        key=lambda e: e['monotonic_us'])
    candidates, app_gaps, native_gaps = [], [], []

    def evidence(a, b):
        lo, hi = (a + offset) * 1000, (b + offset) * 1000
        delays = [e['timestampMs'] + offset - e['monotonic_us'] / 1000
                  for e in telemetry.get('events', []) if a <= e.get('timestampMs', -math.inf) <= b
                  and 'monotonic_us' in e]
        return {'start_s': (a - start) / 1000, 'end_s': (b - start) / 1000,
                'duration_ms': b - a, 'guest_bounds_us': [lo, hi],
                'observed_native_to_browser_delay_ms': [min(delays), max(delays)] if delays else None,
                'relay': counter_evidence(bracket(relay, lo, hi)),
                'subscriber': counter_evidence(bracket(subscriber, lo, hi))}

    for a, b in gaps([e['timestampMs'] for e in eligible], start, pending_end):
        row = evidence(a, b)
        between = [e['monotonic_us'] for e in eligible if a <= e['timestampMs'] <= b and 'monotonic_us' in e]
        row['native_endpoint_object_gap_ms'] = (between[-1] - between[0]) / 1000 if len(between) >= 2 else None
        app_gaps.append(row)
        if b - a >= threshold:
            progress = row['relay'].get('data_sent_bytes', {}).get('delta', 0) > 0
            row = dict(row, kind='application_object_gap', classification=(
                'application_gap_with_native_byte_progress' if progress else 'application_gap_without_observed_native_progress'))
            candidates.append(row)

    native_objects = [e['monotonic_us'] / 1000 - offset for e in eligible if 'monotonic_us' in e]
    for a, b in gaps(native_objects, start, pending_end):
        row = evidence(a, b)
        native_gaps.append(row)
        if b - a >= threshold:
            candidates.append(dict(row, kind='native_object_gap',
                                   classification='no_complete_native_object_requires_byte_and_queue_review'))

    # Independent sender timeline catches stalls masked by buffered browser data.
    for connection in sorted(set(r.get('connection', 'default') for r in relay)):
        rows = [r for r in relay if r.get('connection', 'default') == connection]
        streak_start = None
        for previous, current in zip(rows, rows[1:]):
            lo = max(native_start, previous['monotonic_us'])
            hi = min(native_end, current['monotonic_us'])
            pending = max(previous.get('sendable_bytes', 0), previous.get('queued_stream_bytes', 0),
                          previous.get('picoquic_queued_bytes', 0), previous.get('bytes_in_flight', 0)) > 0
            flat = current.get('data_sent_bytes') == previous.get('data_sent_bytes')
            if hi > lo and pending and flat:
                streak_start = lo if streak_start is None else streak_start
            else:
                if streak_start is not None:
                    add_sender_gap(candidates, rows, streak_start, min(native_end, previous['monotonic_us']),
                                   threshold, offset, start, connection)
                streak_start = None
        if streak_start is not None:
            add_sender_gap(candidates, rows, streak_start, min(native_end, rows[-1]['monotonic_us']),
                           threshold, offset, start, connection)

    limitations = []
    for connection in sorted(set(r.get('connection', 'default') for r in relay)):
        connection_rows = [r for r in relay if r.get('connection', 'default') == connection]
        if any(b.get('data_sent_bytes', 0) < a.get('data_sent_bytes', 0)
               for a, b in zip(connection_rows, connection_rows[1:])
               if native_start <= b['monotonic_us'] <= native_end):
            limitations.append(f'native data_sent_bytes counter decreased: {connection}')
    native_window = [r for r in relay if native_start <= r['monotonic_us'] <= native_end]
    if pending_end > start and len(native_window) < 2:
        limitations.append('insufficient native sender metrics during pending transfer')
    if relay and pending_end > start:
        observed = [native_start] + [r['monotonic_us'] for r in native_window] + [native_end]
        if max(b - a for a, b in zip(observed, observed[1:])) >= threshold * 1000:
            limitations.append('native metric coverage has a gap at least as long as the stall threshold')
    candidates.sort(key=lambda c: c['duration_ms'], reverse=True)
    app_gaps.sort(key=lambda c: c['duration_ms'], reverse=True)
    native_gaps.sort(key=lambda c: c['duration_ms'], reverse=True)
    cadence = [(b['monotonic_ns'] - a['monotonic_ns']) / 1e6
               for a, b in zip(network_samples, network_samples[1:])]
    return {'schema_version': 1, 'status': 'REVIEW' if candidates or limitations else 'PASS',
            'gap_threshold_seconds': gap_seconds, 'observation': {'start_browser_ms': start,
            'end_browser_ms': end, 'duration_ms': duration, 'pending_end_browser_ms': pending_end,
            'guest_bounds_us': [native_start, (end + offset) * 1000]},
            'clock_alignment': clock, 'objects_in_window': len(eligible),
            'excluded_pretrace_objects': sum(e['timestampMs'] < start for e in objects),
            'excluded_posthorizon_objects': sum(e['timestampMs'] > end for e in objects),
            'expected_idle_after_complete_ms': end - pending_end,
            'completion_time_basis': ('last_in_window_object_with_full_horizon_counters' if horizon_counters
                else 'last_in_window_object_with_full_final_counters_and_no_later_objects') if full else None,
            'network_cadence_ms': {'median': statistics.median(cadence), 'max': max(cadence)} if cadence else None,
            'candidates': candidates, 'largest_application_gaps': app_gaps[:8],
            'largest_native_object_gaps': native_gaps[:8], 'limitations': limitations,
            'relay_observation': counter_evidence(native_window),
            'subscriber_observation': counter_evidence([r for r in subscriber
                if native_start <= r['monotonic_us'] <= native_end]),
            'interpretation': 'Subthreshold gaps are retained as evidence. Native byte progress alone does not certify harness health.'}


def add_sender_gap(candidates, rows, lo, hi, threshold, offset, start, connection):
    if hi - lo < threshold * 1000:
        return
    proof = counter_evidence([r for r in rows if lo <= r['monotonic_us'] <= hi])
    eligible = proof.get('sendable_bytes', {}).get('max', 0) > 0
    classification = 'eligible_data_without_send_progress' if eligible else 'pending_data_without_send_progress'
    candidates.append({'kind': 'native_sender_no_progress', 'classification': classification,
                       'connection': connection, 'start_s': (lo / 1000 - offset - start) / 1000,
                       'end_s': (hi / 1000 - offset - start) / 1000, 'duration_ms': (hi - lo) / 1000,
                       'guest_bounds_us': [lo, hi], 'relay': proof})


def analyze_run(run, gap_seconds=1):
    telemetry = json.loads((run / 'player-telemetry.json').read_text())
    evidence_file = run / 'guest-evidence.json'
    directories = json.loads(evidence_file.read_text()).get('directories', []) if evidence_file.exists() else []
    roots = [run] + [Path(p) if Path(p).is_absolute() else run / p for p in directories]
    metric_paths = list(dict.fromkeys(root / 'transport-metrics.csv' for root in roots
                                    if (root / 'transport-metrics.csv').is_file()))
    if len(metric_paths) > 1:
        raise ValueError('multiple relay metric files; select one observation directory')
    rows = []
    if metric_paths:
        with metric_paths[0].open() as stream:
            for row in csv.DictReader(stream):
                rows.append({k: v if k == 'connection' else int(v) for k, v in row.items() if v != ''})
    network_paths = list(dict.fromkeys(root / 'network-samples.jsonl' for root in roots
                                     if (root / 'network-samples.jsonl').is_file()))
    if len(network_paths) > 1:
        raise ValueError('multiple guest clock sample files')
    network = [json.loads(line) for line in network_paths[0].read_text().splitlines()] if network_paths else []
    report = analyze(telemetry, relay_rows=rows, network_samples=network, gap_seconds=gap_seconds)
    report['inputs'] = {'run': str(run.resolve()), 'relay_metrics': [str(p) for p in metric_paths],
                        'network_samples': [str(p) for p in network_paths]}
    return report


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.print_usage(sys.stderr)
            self.exit(1, f'{self.prog}: error: {message}\n')

    parser = Parser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--gap-seconds', type=float, default=1)
    args = parser.parse_args(argv)
    try:
        report = analyze_run(args.run, args.gap_seconds)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f'stall analysis input error: {error}', file=sys.stderr)
        return 1
    print(json.dumps({'status': report['status'], 'candidates': len(report['candidates']),
                      'limitations': report['limitations'], 'output': str(args.output)}))
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
