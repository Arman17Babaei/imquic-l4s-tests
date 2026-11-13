#!/usr/bin/env python3
"""Ideal, shared payload link. No transport stack or kernel shaping is involved.

The virtual-clock API is independent of wall time: call advance(t) before applying
controls at t. It returns serialization and arrival records; controls never consume
link time. The stdio peer uses that same clock, and measures completed pipe writes
against modeled arrival times. A late host or blocked consumer fails the method;
it never changes the configured rate to conceal that disruption.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import selectors
import signal
import struct
import sys
import time

DEFAULT_QUEUE_BYTES = 16 * 1024 * 1024


class QueueBudgetError(ValueError):
    """Configured finite buffering cannot hold the modeled traffic."""


def integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f'invalid {label}: expected integer >= {minimum}')
    return value


def finite(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f'invalid {label}')
    return float(value)


@dataclass(frozen=True)
class ObjectSlice:
    tile_id: str
    refinement: int
    track: str
    object_id: int
    path: Path
    offset: int
    payload_bytes: int
    sha256: str
    publisher_priority: int
    order: tuple

    def verify(self):
        digest = hashlib.sha256()
        remaining = self.payload_bytes
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError('truncated object slice')
                digest.update(block)
                remaining -= len(block)
        if digest.hexdigest() != self.sha256:
            raise ValueError('object hash mismatch')

    def payload(self):
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            data = stream.read(self.payload_bytes)
        if len(data) != self.payload_bytes or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(f'payload hash/size changed: {self.track} object {self.object_id}')
        return data


class ManifestIndex:
    """Validate and hash real slices before readiness; keep payloads on disk."""
    def __init__(self, manifest: Path):
        manifest = Path(manifest).resolve(strict=True)
        self.manifest = manifest
        self.data = json.loads(manifest.read_text())
        self.scene = self.data['scene']['id']
        if not isinstance(self.scene, str) or not self.scene:
            raise ValueError('invalid scene identity')
        self.tracks = {}
        root = manifest.parent
        tiles_seen = set()
        for tile_order, tile in enumerate(self.data['tiles']):
            tile_id = tile['id']
            if not isinstance(tile_id, str) or tile_id in tiles_seen:
                raise ValueError('invalid or duplicate tile identity')
            tiles_seen.add(tile_id)
            refinements_seen = set()
            for layer in tile['refinements']:
                refinement = integer(layer['index'], 'refinement')
                if refinement in refinements_seen:
                    raise ValueError('duplicate refinement')
                refinements_seen.add(refinement)
                track = layer['track']
                if not isinstance(track, str) or not track.startswith(self.scene + '/') or track in self.tracks:
                    raise ValueError('invalid or duplicate track')
                relative = Path(layer['file'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('unsafe payload path')
                path = (root / relative).resolve(strict=True)
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError('unsafe payload path')
                file_size = path.stat().st_size
                objects = []
                seen = set()
                intervals = []
                for description in layer['objects']:
                    object_id = integer(description['id'], 'object ID')
                    offset = integer(description['offset'], 'object offset')
                    size = integer(description['bytes'], 'object size', 1)
                    priority = integer(description.get('publisher_priority', 0), 'publisher priority')
                    if object_id in seen or offset + size > file_size or priority > 255:
                        raise ValueError('invalid object identity, extent or priority')
                    seen.add(object_id)
                    digest = description['sha256']
                    if not isinstance(digest, str) or len(digest) != 64:
                        raise ValueError('invalid object hash')
                    obj = ObjectSlice(tile_id, refinement, track, object_id, path, offset, size, digest, priority, (tile_order, refinement, object_id))
                    obj.verify()
                    objects.append(obj)
                    intervals.append((offset, offset + size))
                intervals.sort()
                if any(a[1] > b[0] for a, b in zip(intervals, intervals[1:])):
                    raise ValueError('overlapping object extents')
                if 'bytes' in layer and integer(layer['bytes'], 'layer bytes') != sum(o.payload_bytes for o in objects):
                    raise ValueError('layer byte count mismatch')
                if 'object_count' in layer and integer(layer['object_count'], 'object count') != len(objects):
                    raise ValueError('object count mismatch')
                if 'sha256' in layer:
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(block)
                    if digest.hexdigest() != layer['sha256']:
                        raise ValueError('layer hash mismatch')
                self.tracks[track] = tuple(sorted(objects, key=lambda o: o.object_id))


class IdealScheduler:
    """Nonpreemptive work-conserving serializer with independent propagation.

    advance() preserves modeled deadlines even after a late caller wakeup. Host
    catch-up therefore appears in dispatch lateness rather than a slower model.
    max_queue_bytes bounds selected active/inflight payload bytes; exceeding it
    raises instead of pausing serialization. Pending objects are metadata only.
    """
    def __init__(self, index, bandwidth_mbps, data_delay_ms, max_queue_bytes=DEFAULT_QUEUE_BYTES):
        self.index = index
        self.rate = finite(bandwidth_mbps, 'bandwidth_mbps', True) * 1e6 / 8
        self.delay = finite(data_delay_ms, 'data_delay_ms') / 1000
        self.max_queue_bytes = integer(max_queue_bytes, 'max_queue_bytes', 1)
        self.fetches = {}
        self.next_request = 1
        self.next_update = 1
        self.active = None
        self.inflight = []
        self.queue_bytes = 0
        self.peak_queue_bytes = 0
        self.cursor = 0.0
        self.closed = False
        self.delivered = set()
        self.sequence = 0

    def open(self, command, now):
        if self.closed or command.get('scene') != self.index.scene:
            raise ValueError('closed link or unknown scene')
        epoch = integer(command.get('epoch', 0), 'epoch')
        controls = []
        for item in command['fetches']:
            track = item['track']
            if track not in self.index.tracks:
                raise ValueError('unknown track')
            objects = self.index.tracks[track]
            tile_id = item['tile_id']
            refinement = integer(item['refinement'], 'refinement')
            if any(f['track'] == track for f in self.fetches.values()):
                raise ValueError('duplicate fetch track')
            if objects and (objects[0].tile_id != tile_id or objects[0].refinement != refinement):
                raise ValueError('fetch identity does not match manifest')
            # Empty layers still have an identity in the original manifest.
            identity = next((t['id'], l['index']) for t in self.index.data['tiles'] for l in t['refinements'] if l['track'] == track)
            if identity != (tile_id, refinement):
                raise ValueError('fetch identity does not match manifest')
            priority = integer(item['priority'], 'priority')
            if priority > 255:
                raise ValueError('invalid priority')
            score = finite(item.get('priority_score', priority), 'priority score')
            if score > 255 or math.floor(score) != priority:
                raise ValueError('priority score must quantize to priority')
            request = self.next_request
            self.next_request += 1
            self.fetches[request] = dict(track=track, tile_id=tile_id, refinement=refinement, priority=priority, score=score, epoch=epoch, pending=deque(objects), remaining=len(objects))
            controls.append(dict(type='fetch-opened', tile_id=tile_id, refinement=refinement, request_id=request, initial_priority=priority, initial_priority_score=score, epoch=epoch))
            if not objects:
                controls.append(dict(type='fetch-status', state='complete', request_id=request, tile_id=tile_id, refinement=refinement))
        if self.active is None:
            self.cursor = max(self.cursor, now)
        return controls

    def update(self, command, now):
        if self.closed:
            raise ValueError('closed link')
        epoch = integer(command.get('epoch', 0), 'epoch')
        controls = []
        for item in command['updates']:
            request = integer(item['request_id'], 'request ID', 1)
            if request not in self.fetches:
                raise ValueError('unknown request ID')
            priority = integer(item['priority'], 'priority')
            if priority > 255:
                raise ValueError('invalid priority')
            score = finite(item.get('priority_score', priority), 'priority score')
            if score > 255 or math.floor(score) != priority:
                raise ValueError('priority score must quantize to priority')
            fetch = self.fetches[request]
            old = fetch['priority']
            fetch['priority'], fetch['score'], fetch['epoch'] = priority, score, epoch
            update_id = self.next_update
            self.next_update += 1
            controls.append(dict(type='priority-accepted', request_id=request, update_id=update_id, tile_id=fetch['tile_id'], refinement=fetch['refinement'], old_priority=old, new_priority=priority, new_priority_score=score, epoch=epoch))
        return controls

    def _select(self, start):
        eligible = [(f['priority'], f['score'], f['pending'][0].order, request) for request, f in self.fetches.items() if f['pending']]
        if not eligible:
            return None
        _, _, _, request = min(eligible)
        fetch = self.fetches[request]
        obj = fetch['pending'][0]
        if self.queue_bytes + obj.payload_bytes > self.max_queue_bytes:
            raise QueueBudgetError('ideal inflight queue budget exceeded; model cannot be slowed')
        fetch['pending'].popleft()
        self.queue_bytes += obj.payload_bytes
        self.peak_queue_bytes = max(self.peak_queue_bytes, self.queue_bytes)
        finish = start + obj.payload_bytes / self.rate
        event = dict(event='serialization', tile_id=obj.tile_id, refinement=obj.refinement, track=obj.track, object_id=obj.object_id, request_id=request, payload_bytes=obj.payload_bytes, publisher_priority=obj.publisher_priority, subscriber_priority=fetch['priority'], subscriber_score=fetch['score'], subscriber_epoch=fetch['epoch'], sha256=obj.sha256, start=start, finish=finish, arrival=finish + self.delay, object=obj)
        self.active = event
        self.sequence += 1
        heapq.heappush(self.inflight, (event['arrival'], self.sequence, event))
        return event

    def advance(self, now):
        finite(now, 'clock')
        events = []
        if self.closed:
            return events
        while True:
            # Retire propagation first when arrival coincides with a boundary.
            if self.inflight and self.inflight[0][0] <= now and (self.active is None or self.inflight[0][0] <= self.active['finish']):
                _, _, selected = heapq.heappop(self.inflight)
                event = dict(selected, event='arrival')
                key = (event['request_id'], event['object_id'])
                if key in self.delivered:
                    raise ValueError('duplicate modeled delivery')
                self.delivered.add(key)
                self.queue_bytes -= event['payload_bytes']
                fetch = self.fetches[event['request_id']]
                fetch['remaining'] -= 1
                event['fetch_complete'] = fetch['remaining'] == 0
                events.append(event)
                continue
            if self.active is not None:
                if self.active['finish'] > now:
                    break
                self.cursor = self.active['finish']
                self.active = None
            if self.cursor <= now:
                selected = self._select(self.cursor)
                if selected:
                    events.append(selected)
                    continue
            if self.inflight and self.inflight[0][0] <= now:
                continue
            break
        return events

    def next_deadline(self):
        deadlines = [self.active['finish']] if self.active else []
        if self.inflight:
            deadlines.append(self.inflight[0][0])
        if self.active is None and any(f['pending'] for f in self.fetches.values()):
            deadlines.append(self.cursor)
        return min(deadlines) if deadlines else None

    def cancel(self, now):
        cancelled = []
        for _, _, selected in self.inflight:
            cancelled.append(dict(selected, event='cancel', cancel_time=now))
        for request, fetch in self.fetches.items():
            for obj in fetch['pending']:
                cancelled.append(dict(event='cancel', tile_id=obj.tile_id, refinement=obj.refinement, object_id=obj.object_id, request_id=request, payload_bytes=obj.payload_bytes, cancel_time=now))
            fetch['pending'].clear()
        self.inflight.clear()
        self.active = None
        self.queue_bytes = 0
        self.closed = True
        return cancelled


def frame(header, payload=b''):
    encoded = json.dumps(header, separators=(',', ':')).encode()
    return struct.pack('!I', len(encoded)) + encoded + payload


def run(config):
    output = Path(config['output'])
    if not output.is_absolute() or not Path(config['manifest']).is_absolute():
        raise ValueError('manifest and output must be absolute paths')
    output.mkdir(parents=True, exist_ok=True)
    events_file = (output / 'ideal-events.jsonl').open('w', buffering=65536)
    started = time.monotonic()
    clock_origin_unix_ms = time.time_ns() / 1e6
    summary = dict(status='FAIL', transport='ideal', clock_origin_monotonic_us=int(started * 1e6), clock_origin_unix_ms=clock_origin_unix_ms, max_queue_bytes=config.get('max_queue_bytes', DEFAULT_QUEUE_BYTES), bandwidth_mbps=config['bandwidth_mbps'], data_delay_ms=config['data_delay_ms'], delivered_objects=0, delivered_payload_bytes=0, early_deliveries=0, budget_violations=0, peak_queue_bytes=0, peak_output_bytes=0, failures=[], invariants=dict(shared_payload_rate=True, nonpreemptive_objects=True, control_charged_bytes=0, independent_propagation=True, priorities_pending_only=True))
    lateness = []
    scheduler = None
    outgoing = deque()
    output_bytes = 0
    stop = False
    signal_stop = False
    selector = selectors.DefaultSelector()
    previous_signals = {}

    def log(event):
        clean = {k: v for k, v in event.items() if k != 'object'}
        clean['monotonic_s'] = time.monotonic()
        clean['model_clock_origin_monotonic_s'] = started
        events_file.write(json.dumps(clean, separators=(',', ':')) + '\n')

    def fail(message):
        summary['failures'].append(str(message))

    def emit(header, payload=b'', arrival=None, completion=None):
        nonlocal output_bytes
        encoded = frame(header, payload)
        if output_bytes + len(encoded) + (scheduler.queue_bytes if scheduler else 0) > config.get('max_queue_bytes', DEFAULT_QUEUE_BYTES):
            raise QueueBudgetError('ideal output queue budget exceeded')
        outgoing.append(dict(data=encoded, offset=0, arrival=arrival, header=header, completion=completion))
        output_bytes += len(encoded)
        summary['peak_output_bytes'] = max(summary['peak_output_bytes'], output_bytes)
        if not selector.get_map().get(sys.stdout.fileno()):
            selector.register(sys.stdout.fileno(), selectors.EVENT_WRITE, 'output')

    def control(message):
        message = dict(message, monotonic_us=int(time.monotonic() * 1e6))
        log(dict(event='control', message=message))
        emit(dict(wire='control', message=message))

    def control_updates(messages, batch_size):
        # Same scheduler updates and raw request-level evidence, fewer wire frames.
        for offset in range(0, len(messages), batch_size):
            group = []
            for message in messages[offset:offset + batch_size]:
                stamped = dict(message, monotonic_us=int(time.monotonic() * 1e6))
                log(dict(event='control', message=stamped))
                group.append({key: stamped[key] for key in ('request_id', 'update_id', 'new_priority', 'new_priority_score', 'monotonic_us')})
            emit(dict(wire='control', message=dict(type='priorities-accepted',
                epoch=messages[offset]['epoch'], updates=group)))

    def consume(events):
        for event in events:
            log(event)
            if event['event'] != 'arrival':
                continue
            payload = event['object'].payload()
            header = {key: event[key] for key in ('tile_id', 'refinement', 'object_id', 'request_id', 'publisher_priority', 'payload_bytes', 'subscriber_priority', 'subscriber_score', 'subscriber_epoch')}
            header.update(wire='object', monotonic_us=int(time.monotonic() * 1e6),
                ideal_arrival_monotonic_us=int((started + event['arrival']) * 1e6),
                ideal_arrival_unix_ms=clock_origin_unix_ms + event['arrival'] * 1000)
            completion = None
            if event['fetch_complete']:
                completion = dict(type='fetch-status', state='complete', request_id=event['request_id'], tile_id=event['tile_id'], refinement=event['refinement'])
            emit(header, payload, event['arrival'], completion)

    def close(now):
        nonlocal stop, output_bytes
        for event in scheduler.cancel(now):
            log(event)
        retained = deque()
        for pending in outgoing:
            if pending['arrival'] is not None and pending['offset'] == 0:
                output_bytes -= len(pending['data'])
                log(dict(event='cancel-output', **pending['header']))
            else:
                if pending['arrival'] is not None:
                    fail('close interrupted an object pipe write')
                retained.append(pending)
        outgoing.clear()
        outgoing.extend(retained)
        control(dict(type='audit-complete', unresolved_updates=0))
        stop = True

    def interrupted(signum, _frame):
        nonlocal signal_stop
        signal_stop = True
        fail(f'interrupted by signal {signum}')

    try:
        batch_size = integer(config.get('priority_control_batch_size', 0), 'priority control batch size')
        if batch_size > 128:
            raise ValueError('priority control batches exceed header bound')
        summary['priority_control_batch_size'] = batch_size
        index = ManifestIndex(Path(config['manifest']))
        scheduler = IdealScheduler(index, config['bandwidth_mbps'], config['data_delay_ms'], config.get('max_queue_bytes', DEFAULT_QUEUE_BYTES))
        os.set_blocking(sys.stdin.fileno(), False)
        os.set_blocking(sys.stdout.fileno(), False)
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ, 'input')
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_signals[sig] = signal.signal(sig, interrupted)
        control(dict(type='ready', transport='ideal'))
        input_buffer = bytearray()
        close_started = None
        while not signal_stop:
            now = time.monotonic() - started
            if not stop:
                consume(scheduler.advance(now))
            if stop and not outgoing:
                break
            if stop and close_started is None:
                close_started = time.monotonic()
            if close_started is not None and time.monotonic() - close_started > .5:
                raise ValueError('stdout backpressure prevented close')
            deadline = None if stop else scheduler.next_deadline()
            timeout = .05 if deadline is None else min(.05, max(0, deadline - (time.monotonic() - started)))
            ready = selector.select(timeout)
            # Controls win when the input and output descriptors wake together.
            ready.sort(key=lambda pair: pair[0].data != 'input')
            for key, _ in ready:
                if key.data == 'input':
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        selector.unregister(sys.stdin.fileno())
                        if not stop:
                            close(time.monotonic() - started)
                        continue
                    input_buffer.extend(chunk)
                    if len(input_buffer) > 1024 * 1024:
                        raise ValueError('command buffer exceeds 1 MiB')
                    while b'\n' in input_buffer and not stop:
                        line, _, rest = input_buffer.partition(b'\n')
                        input_buffer[:] = rest
                        command = json.loads(line)
                        now = time.monotonic() - started
                        if command.get('type') == 'close':
                            consume(scheduler.advance(now))
                            close(now)
                            break
                        consume(scheduler.advance(now))
                        if command.get('type') == 'open':
                            controls = scheduler.open(command, now)
                        elif command.get('type') == 'priorities':
                            controls = scheduler.update(command, now)
                        else:
                            raise ValueError('unknown ideal control command')
                        if command.get('type') == 'priorities' and batch_size:
                            control_updates(controls, batch_size)
                        else:
                            for message in controls:
                                control(message)
                elif outgoing:
                    pending = outgoing[0]
                    try:
                        written = os.write(sys.stdout.fileno(), memoryview(pending['data'])[pending['offset']:])
                    except BlockingIOError:
                        continue
                    pending['offset'] += written
                    output_bytes -= written
                    if pending['offset'] == len(pending['data']):
                        outgoing.popleft()
                        if pending['arrival'] is not None:
                            actual = time.monotonic() - started
                            late = actual - pending['arrival']
                            if late < -1e-9:
                                summary['early_deliveries'] += 1
                            lateness.append(max(0, late) * 1000)
                            summary['delivered_objects'] += 1
                            summary['delivered_payload_bytes'] += pending['header']['payload_bytes']
                            log(dict(event='dispatch', **pending['header'], arrival=pending['arrival'], actual=actual, lateness_ms=late * 1000))
                            if pending['completion'] and not stop:
                                control(pending['completion'])
                    if not outgoing:
                        selector.unregister(sys.stdout.fileno())
        if signal_stop and scheduler:
            for event in scheduler.cancel(time.monotonic() - started):
                log(event)
    except Exception as error:
        if isinstance(error, QueueBudgetError):
            summary['budget_violations'] += 1
        fail(f'{type(error).__name__}: {error}')
        print(f'ideal link failed: {error}', file=sys.stderr)
        if scheduler and not scheduler.closed:
            for event in scheduler.cancel(time.monotonic() - started):
                log(event)
    finally:
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)
        selector.close()
        ordered = sorted(lateness)
        p95 = ordered[max(0, math.ceil(len(ordered) * .95) - 1)] if ordered else 0
        maximum = max(ordered, default=0)
        summary.update(dispatch_lateness_p95_ms=p95, dispatch_lateness_max_ms=maximum, dispatch_samples=len(ordered), peak_queue_bytes=scheduler.peak_queue_bytes if scheduler else 0)
        if p95 > 5 or maximum > 20:
            fail('dispatch lateness exceeded p95 <= 5 ms / max <= 20 ms')
        if not summary['failures'] and not summary['early_deliveries'] and not summary['budget_violations']:
            summary['status'] = 'PASS'
        (output / 'ideal-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        events_file.close()
    return 0 if summary['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    return run(json.loads(args.config.read_text()))


if __name__ == '__main__':
    sys.exit(main())
