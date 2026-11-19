#!/usr/bin/env python3
"""Deterministic offline payload clock over actual stepped IMQUIC delivery.

No wall-clock timer schedules frames or payload availability. Native QUIC carries
one permitted object at a time; bandwidth and propagation are accounted in bytes
and rational simulated time after its actual IMQUIC receipt.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
from pathlib import Path
import signal
import struct
import sys

from ideal_imquic import ImquicPeer

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



def frame(header, payload=b''):
    encoded = json.dumps(header, separators=(',', ':')).encode()
    return struct.pack('!I', len(encoded)) + encoded + payload


class OfflineDelivery:
    """Rational payload accounting around synchronous IMQUIC object selection."""
    def __init__(self, peer, bandwidth_mbps, data_delay_ms, max_queue_bytes=DEFAULT_QUEUE_BYTES):
        from fractions import Fraction
        self.peer = peer
        self.rate = Fraction(str(finite(bandwidth_mbps, 'bandwidth_mbps', True))) * 1_000_000 / 8
        self.delay = Fraction(str(finite(data_delay_ms, 'data_delay_ms'))) / 1000
        self.now = self.cursor = Fraction(0)
        self.active = None
        self.inflight = []
        self.sequence = self.queue_bytes = self.peak_queue_bytes = 0
        self.max_queue_bytes = integer(max_queue_bytes, 'max_queue_bytes', 1)

    def open(self, command):
        return self.peer.open(command)

    def update(self, command):
        return self.peer.update(command)

    def advance(self, now):
        if now < self.now:
            raise ValueError('simulated time must not decrease')
        events = []
        while self.cursor <= now:
            if self.active is not None:
                if self.active > now:
                    break
                self.cursor, self.active = self.active, None
                continue
            selected = self.peer.next_object()
            if selected is None:
                self.cursor = now  # Idle capacity is never banked.
                break
            header, payload = selected
            finish = self.cursor + len(payload) / self.rate
            arrival = finish + self.delay
            self.sequence += 1
            event = dict(header, start=float(self.cursor), finish=float(finish), arrival=float(arrival))
            # Retire arrived payloads before charging the next selected object.
            while self.inflight and self.inflight[0][0] <= self.cursor:
                _, _, due, data = heapq.heappop(self.inflight)
                self.queue_bytes -= len(data)
                events.append(dict(due, event='arrival', payload=data))
            if self.queue_bytes + len(payload) > self.max_queue_bytes:
                raise QueueBudgetError('ideal inflight queue budget exceeded')
            self.queue_bytes += len(payload)
            self.peak_queue_bytes = max(self.peak_queue_bytes, self.queue_bytes)
            heapq.heappush(self.inflight, (arrival, self.sequence, event, payload))
            self.active = finish
            events.append(dict(event, event='serialization'))
        while self.inflight and self.inflight[0][0] <= now:
            _, _, event, payload = heapq.heappop(self.inflight)
            self.queue_bytes -= len(payload)
            events.append(dict(event, event='arrival', payload=payload))
        self.now = now
        return events


def run(config):
    """Blocking command protocol. Waiting and pipe backpressure cost no model time."""
    from fractions import Fraction
    output = Path(config['output'])
    manifest = Path(config['manifest'])
    if not output.is_absolute() or not manifest.is_absolute():
        raise ValueError('manifest and output must be absolute paths')
    output.mkdir(parents=True, exist_ok=True)
    summary = dict(status='FAIL', transport='ideal', delivery_model='offline-imquic-v1',
        bandwidth_mbps=config['bandwidth_mbps'], data_delay_ms=config['data_delay_ms'],
        time_origin='initial-request', delivered_objects=0, delivered_payload_bytes=0,
        budget_violations=0, failures=[], peak_queue_bytes=0)
    model = None
    closed = False
    def interrupted(signum, _frame):
        raise InterruptedError(f'ideal driver interrupted by signal {signum}')
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    with (output / 'ideal-events.jsonl').open('w') as events_file:
        def log(event):
            events_file.write(json.dumps({k: v for k, v in event.items() if k != 'payload'}, separators=(',', ':')) + '\n')
        def emit(header, payload=b''):
            sys.stdout.buffer.write(frame(header, payload))
            sys.stdout.buffer.flush()
        def control(message):
            # Native operational timestamps are not part of simulated provenance.
            clean = {k: v for k, v in message.items() if k != 'monotonic_us'}
            log(dict(event='control', message=clean))
            emit(dict(wire='control', message=clean))
        try:
            ManifestIndex(manifest)  # Validate every encoded slice before native publication.
            with ImquicPeer(manifest, output) as peer:
                model = OfflineDelivery(peer, config['bandwidth_mbps'], config['data_delay_ms'],
                    config.get('max_queue_bytes', DEFAULT_QUEUE_BYTES))
                control(dict(type='ready', transport='ideal', delivery_model='offline-imquic-v1'))
                for line in sys.stdin:
                    if len(line) > 1024 * 1024:
                        raise ValueError('oversized ideal command')
                    command = json.loads(line)
                    kind = command.get('type')
                    if kind == 'close':
                        closed = True
                        break
                    if kind == 'open':
                        for message in model.open(command):
                            control(message)
                    elif kind == 'priorities':
                        for message in model.update(command):
                            control(message)
                    elif kind in ('advance', 'horizon'):
                        if kind == 'advance':
                            index = integer(command['frame_index'], 'frame index')
                            fps = integer(command['fps'], 'fps', 1)
                            now = Fraction(index, fps)
                        else:
                            index = -1
                            now = Fraction(str(finite(command['duration_ms'], 'horizon', True))) / 1000
                        for event in model.advance(now):
                            log(event)
                            if event['event'] != 'arrival':
                                continue
                            header = {k: v for k, v in event.items() if k not in ('payload', 'event')}
                            header.update(wire='object', monotonic_us=0,
                                ideal_arrival_ms=event['arrival'] * 1000)
                            emit(header, event['payload'])
                            summary['delivered_objects'] += 1
                            summary['delivered_payload_bytes'] += len(event['payload'])
                            log(dict(header, event='dispatch'))
                            if event['fetch_complete']:
                                control(dict(type='fetch-status', state='complete', request_id=event['request_id'],
                                    tile_id=event['tile_id'], refinement=event['refinement']))
                        control(dict(type='advance-complete', frame_index=index, timestamp_ms=float(now * 1000)))
                    else:
                        raise ValueError('unknown ideal command')
                if not closed:
                    raise ValueError('ideal driver input closed before audit')
            summary['status'] = 'PASS'
        except Exception as error:
            summary['failures'].append(f'{type(error).__name__}: {error}')
            summary['budget_violations'] += isinstance(error, QueueBudgetError)
            print(f'ideal link failed: {error}', file=sys.stderr)
            try:
                control(dict(type='error', message=str(error)))
            except (OSError, BrokenPipeError):
                pass
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if model:
                summary.update(peak_queue_bytes=model.peak_queue_bytes,
                    simulated_time_ms=float(model.now * 1000))
            (output / 'ideal-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        if summary['status'] == 'PASS':
            control(dict(type='audit-complete', unresolved_updates=0))
    return 0 if summary['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    return run(json.loads(args.config.read_text()))


if __name__ == '__main__':
    sys.exit(main())
