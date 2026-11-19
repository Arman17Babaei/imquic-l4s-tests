"""Synchronous object selection through the real IMQUIC relay and subscriber.

Local QUIC is a reliable carrier, not the modeled link. Each selection is gated
at the relay, after all previous controls have been acknowledged. No host time
enters the simulated object schedule.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import queue
import socket
import struct
import subprocess
import threading

ROOT = Path(__file__).resolve().parents[2]


class ImquicPeer:
    def __init__(self, manifest: Path, output: Path):
        self.manifest, self.output = Path(manifest), Path(output)
        self.processes, self.logs = [], []
        self.messages = queue.Queue()
        self.requests = {}
        self.complete = set()
        self.objects = {}
        data = json.loads(self.manifest.read_text())
        for tile in data['tiles']:
            for layer in tile['refinements']:
                for obj in layer['objects']:
                    self.objects[tile['id'], layer['index'], obj['id']] = obj

    def _start(self, name, command, **kwargs):
        log = (self.output / f'ideal-{name}.log').open('wb')
        self.logs.append(log)
        process = subprocess.Popen(command, cwd=ROOT, stderr=log, **kwargs)
        self.processes.append(process)
        return process

    def __enter__(self):
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            # Separate descriptor: relay logs never share the control protocol.
            self.control, remote = socket.socketpair()
            self.control.settimeout(180)
            self.control_file = self.control.makefile('rwb', buffering=0)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = str(reservation.getsockname()[1])
            with remote:
                self.relay = self._start('relay', [str(ROOT / 'deps/imquic/examples/imquic-moq-relay'),
                    '-M', '19', '-q', '-b', '127.0.0.1', '-p', port,
                    '-c', str(ROOT / 'deps/picoquic/certs/cert.pem'),
                    '-k', str(ROOT / 'deps/picoquic/certs/key.pem'), '-Z', '-T', '-d', '1'],
                    stdout=subprocess.DEVNULL, pass_fds=(remote.fileno(),),
                    env={**os.environ, 'IMQUIC_RELAY_IDEAL_FD': str(remote.fileno())})
            if self._reply() != 'ready':
                raise RuntimeError('IMQUIC relay did not enable deterministic stepping')
            self.publisher = self._start('publisher', [str(ROOT / 'build/priority-moq-publisher'),
                '127.0.0.1', port, str(self.manifest)], stdout=subprocess.DEVNULL)
            # Cache readiness is measured at the relay, not inferred from sends.
            self._request(f'cache {len(self.objects)}')
            if self._reply() != f'cached {len(self.objects)}':
                raise RuntimeError('IMQUIC relay cache does not match manifest')
            self.native = self._start('subscriber', [str(ROOT / 'build/priority-moq-sidecar'),
                '127.0.0.1', port, 'reno'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self.reader = threading.Thread(target=self._read_native, daemon=True)
            self.reader.start()
            self._until(lambda h: h.get('type') == 'ready')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _request(self, line):
        self.control_file.write((line + '\n').encode())

    def _reply(self):
        line = self.control_file.readline(256)
        if not line:
            raise RuntimeError('IMQUIC relay control disconnected')
        return line.decode().strip()

    def _read_native(self):
        try:
            def read(size):
                data = self.native.stdout.read(size)
                if len(data) != size:
                    raise RuntimeError('IMQUIC subscriber disconnected')
                return data
            while True:
                length, = struct.unpack('!I', read(4))
                if length > 65536:
                    raise ValueError('oversized IMQUIC envelope')
                header = json.loads(read(length))
                payload = read(header.get('payload_bytes', 0))
                self.messages.put((header, payload))
        except Exception as error:
            self.messages.put(error)

    def _until(self, predicate):
        controls = []
        while True:
            try:
                item = self.messages.get(timeout=180)
            except queue.Empty as error:
                raise TimeoutError('IMQUIC step did not complete; see ideal native logs') from error
            if isinstance(item, Exception):
                raise item
            header, payload = item
            if header.get('wire') == 'control':
                message = header['message']
                if message['type'] == 'transport-metrics':
                    continue
                if message['type'] in ('error', 'priority-failed') or (
                        message['type'] == 'fetch-status' and message['state'] == 'failed'):
                    raise RuntimeError(f'IMQUIC control failed: {message}')
                controls.append(message)
                if predicate(message):
                    return controls
            else:
                if not predicate(header):
                    raise RuntimeError('IMQUIC delivered an object without a selection permit')
                return header, payload

    def _command(self, command):
        self.native.stdin.write((json.dumps(command) + '\n').encode())
        self.native.stdin.flush()

    def open(self, command):
        self._command(command)
        opened, accepted, controls = {}, set(), []
        expected = len(command['fetches'])
        while len(accepted) < expected:
            events = self._until(lambda m: m['type'] == 'fetch-status' and m['state'] == 'accepted')
            for event in events:
                if event['type'] == 'fetch-opened':
                    opened[event['request_id']] = event
                if event['type'] == 'fetch-status' and event['state'] == 'accepted':
                    accepted.add(event['request_id'])
            controls.extend(events)
        if len(opened) != expected:
            raise RuntimeError('IMQUIC FETCH acknowledgement mismatch')
        for item, event in zip(command['fetches'], opened.values()):
            self.requests[event['request_id']] = dict(priority=item['priority'], epoch=command.get('epoch', 0))
        return controls

    def update(self, command):
        pending = [u for u in command['updates'] if u['request_id'] not in self.complete and
                   self.requests[u['request_id']]['priority'] != u['priority']]
        if not pending:
            return []
        self._command(dict(command, updates=pending))
        accepted, controls = set(), []
        while len(accepted) < len(pending):
            events = self._until(lambda m: m['type'] == 'priority-accepted')
            controls.extend(events)
            event = events[-1]
            accepted.add(event['request_id'])
            self.requests[event['request_id']] = dict(priority=event['new_priority'], epoch=event['epoch'])
        return controls

    def next_object(self):
        if len(self.complete) == len(self.requests):
            return None
        self._request('next')
        reply = self._reply().split()
        if reply == ['empty']:
            if len(self.complete) != len(self.requests):
                raise RuntimeError('IMQUIC relay exhausted before all FETCHes completed')
            return None
        if len(reply) != 5 or reply[0] != 'object':
            raise RuntimeError(f'IMQUIC selection failed: {reply}')
        request, object_id, size, priority = map(int, reply[1:])
        header, payload = self._until(lambda h: 'payload_bytes' in h)
        if (header['request_id'], header['object_id'], len(payload), header['subscriber_priority']) != (request, object_id, size, priority):
            raise RuntimeError('IMQUIC selected and received object disagree')
        identity = header['tile_id'], header['refinement'], object_id
        expected = self.objects.pop(identity)
        if len(payload) != expected['bytes'] or hashlib.sha256(payload).hexdigest() != expected['sha256']:
            raise ValueError('IMQUIC payload hash/size differs from manifest')
        if not any(key[:2] == identity[:2] for key in self.objects):
            self.complete.add(request)
        header['fetch_complete'] = request in self.complete
        header['sha256'] = expected['sha256']
        header.pop('monotonic_us', None)
        return header, payload

    def __exit__(self, *_):
        if hasattr(self, 'native') and self.native.poll() is None:
            try:
                self._command(dict(type='close'))
                self.native.stdin.close()
                self.native.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if hasattr(self, 'control_file'):
            self.control_file.close()
            self.control.close()
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
        if hasattr(self, 'reader'):
            self.reader.join(timeout=2)
        for log in self.logs:
            log.close()
