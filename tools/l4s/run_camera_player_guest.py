#!/usr/bin/env python3
"""Expose the priority MoQ bridge to a host browser through QEMU forwarding."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shlex
import signal
import json
import os
import subprocess
import sys
import time
import re
import threading
from pathlib import Path

from experiment_metadata import capture_mininet_state, write_experiment_record
ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / 'tools/l4s/priority_ws_bridge.mjs'
NODE = os.environ.get('CAMERA_PRIORITY_NODE', 'node')
PUBLISHER = ROOT / 'build/priority-moq-publisher'
RELAY = ROOT / 'deps/imquic/examples/imquic-moq-relay'
SIDECAR = ROOT / 'build/priority-moq-sidecar'


def load_topology(path: Path):
    spec = importlib.util.spec_from_file_location('player_topology_run', path)
    if spec is None or spec.loader is None:
        raise ValueError(f'cannot load topology script: {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


def wait_path(path, processes, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file(): return
        if any(process.poll() is not None for process in processes):
            raise RuntimeError(f'process exited before readiness: {path}')
        time.sleep(.1)
    raise TimeoutError(f'publisher readiness timed out: {path}')


def device_command(node, command):
    if node is None:
        return subprocess.check_output(command, text=True)
    # Each command owns a process: Mininet's shared cmd shell is unsafe while
    # the telemetry thread and the main evidence collector run concurrently.
    process = node.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output, _ = process.communicate(timeout=10)
    if process.returncode:
        raise RuntimeError(f'{shlex.join(command)}: {output}')
    return output


def parse_qdisc_stats(text):
    """Extract compact AQM statistics, preserving raw output separately."""
    result = {}
    patterns = {
        'backlog_bytes': r'backlog (\d+)b',
        'backlog_packets': r'backlog \d+b (\d+)p',
        'drops': r'dropped (\d+)',
        'ecn_mark': r'\becn_mark (\d+)',
        'step_marks': r'\bstep_marks (\d+)',
    }
    for name, pattern in patterns.items():
        values = re.findall(pattern, text)
        if values:
            # HTB and its child both report queue totals; use the leaf value.
            result[name] = int(values[-1])
    for name in ('delay_c', 'delay_l'):
        match = re.search(rf'\b{name} ([\d.]+)(us|ms|s)', text)
        if match:
            result[f'{name}_us'] = float(match[1]) * {'us': 1, 'ms': 1000, 's': 1000000}[match[2]]
    return result


class NetworkSampler:
    """Guest-only 100ms snapshots of two AQMs and media endpoint counters.

    Timestamps bound each snapshot so downstream analysis can measure actual
    cadence and align guest wall clock with browser audit time.
    """
    def __init__(self, output, devices, roles):
        self.path = output / 'network-samples.jsonl'
        self.devices = {role: devices[role] for role in roles}
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.run, name='network-sampler', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.done.set()
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise RuntimeError('network sampler did not stop')

    def check(self):
        if self.error is not None:
            raise RuntimeError(f'network sampler failed: {self.error}')

    def run(self):
        try:
            with self.path.open('w') as stream:
                deadline = time.monotonic()
                while not self.done.is_set():
                    record = {'unix_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns(), 'devices': {}}
                    for role, (node, device) in self.devices.items():
                        entry = {'device': device, 'link': counters(node, device)}
                        if role in ('provider_egress', 'downstream_egress'):
                            raw = device_command(node, ['tc', '-s', '-d', 'qdisc', 'show', 'dev', device])
                            entry.update({'qdisc': parse_qdisc_stats(raw), 'qdisc_raw': raw})
                        record['devices'][role] = entry
                    record['end_monotonic_ns'] = time.monotonic_ns()
                    stream.write(json.dumps(record, separators=(',', ':')) + '\n')
                    stream.flush()
                    deadline += .1
                    # Skip missed ticks rather than replaying samples in a burst.
                    deadline = max(deadline, time.monotonic())
                    self.done.wait(max(0, deadline - time.monotonic()))
        except BaseException as error:
            self.error = error


def counters(node, device):
    return json.loads(device_command(node, ['ip', '-s', '-j', 'link', 'show', 'dev', device]))[0]


def write_tc(output, phase, devices):
    with (output / f'tc-state-{phase}.txt').open('w') as stream:
        for name, (node, device) in devices.items():
            stream.write(f'[{name}] device={device}\n')
            for kind in ('qdisc', 'class'):
                stream.write(device_command(node, ['tc', '-s', '-d', kind, 'show', 'dev', device]) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--transport-mode', choices=('prague', 'reno'), required=True)
    parser.add_argument('--duration', type=float, default=600)
    parser.add_argument('--origin', default='')
    parser.add_argument('--admission-delay-us', type=int, default=0)
    parser.add_argument('--bottleneck', default=None, help='Optional override of the topology script rate')
    parser.add_argument('--topology-script', type=Path, default=ROOT / 'tools/l4s/player_topology.py')
    parser.add_argument('--persistent', action='store_true')
    parser.add_argument('--admission-queue-bytes', type=int, default=262144)
    args = parser.parse_args()
    if not 65536 <= args.admission_queue_bytes <= 2097152:
        parser.error('--admission-queue-bytes must be in 65536..2097152')
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ['CAMERA_PLAYER_MODE'] = args.transport_mode
    topology = load_topology(args.topology_script.resolve())
    net, client, server, devices = topology.build_topology()
    processes, handles = [], []
    background = []
    sampler = None
    def launch(node, name, command, env=None):
        if node is None:
            handle = (args.output / f'{name}.log').open('wb')
            process = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env)
        else:
            handle = (args.output / f'{name}.log').open('wb')
            process = node.popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env)
        processes.append(process); handles.append(handle)
        return process
    def evidence(phase):
        write_tc(args.output, phase, devices)
        (args.output / f'interface-counters-{phase}.json').write_text(json.dumps({
            name: counters(node, device) for name, (node, device) in devices.items()}, indent=2) + '\n')
    try:
        net.start()
        topology.configure_topology(devices, args.bottleneck or None)
        if hasattr(topology, 'start_background'):
            background = topology.start_background(devices, args.output)
            for process, handle in background:
                processes.append(process); handles.append(handle)
        if hasattr(topology, 'METRIC_ROLES'):
            sampler = NetworkSampler(args.output, devices, topology.METRIC_ROLES)
            sampler.start()
        evidence('before')
        write_experiment_record(args.output, scenario='camera-player-qemu',
            configuration={'manifest': str(args.manifest), 'controller': args.transport_mode,
                'duration_limit_seconds': args.duration, 'rate_override': args.bottleneck,
                'topology_script': str(args.topology_script),
                'topology_sha256': hashlib.sha256(args.topology_script.read_bytes()).hexdigest(),
                'admission_delay_us': args.admission_delay_us,
                'admission_queue_bytes': args.admission_queue_bytes,
                'relay_congestion_options': 'pacing_interval=1/1000,sudden_ce_threshold=off' if args.transport_mode == 'prague' else '',
                'subscriber_congestion_options': 'pacing_interval=1/1000,sudden_ce_threshold=1/1' if args.transport_mode == 'prague' else '',
                'udp_send_buffer_bytes': 1048576, 'udp_receive_buffer_bytes': 1048576,
                'stream_receive_window_bytes': 1048576, 'packet_train_mode': 0,
                'persistent_bridge': args.persistent, 'subscriber_start': 'browser Play' if args.persistent else 'immediate',
                'background_flows': len(background) // 2,
                'network_sample_interval_seconds': .1 if sampler else None,
                'question': 'Does the host browser receive hashed SH3 MoQ objects through guest DualPI2?',
                'hypothesis': 'Browser-applied objects match the publisher and all data crosses shaped guest interfaces.',
                'control': 'Reno with the same scene and topology', 'capture_mode': 'transport-metrics'},
            topology={'path': 'host browser -> QEMU TCP forward -> guest proxy -> client bridge/subscriber -> topology script network -> server relay/publisher',
                'nodes': [node.name for node in net.hosts + net.switches],
                'moq_client': client.IP(), 'moq_server': server.IP(), 'relay_port': 4443, 'guest_forward_port': 7790},
            observed_network={switch.name: capture_mininet_state(client, server, switch) for switch in net.switches})
        certificate, key = args.output / 'cert.pem', args.output / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=10.0.0.2',
            '-keyout', str(key), '-out', str(certificate), '-days', '1'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        relay = launch(server, 'relay', [str(RELAY), '-M', '19', '-q', '-b', server.IP(), '-p', '4443',
            '-c', str(certificate), '-k', str(key), '-Z', '-T', '-d', '4', '--congestion-control',
            args.transport_mode, '--transport-metrics', str(args.output / 'transport-metrics.csv')],
            env={**os.environ, 'IMQUIC_RELAY_ADMISSION_DELAY_US': str(args.admission_delay_us),
                 'IMQUIC_RELAY_ADMISSION_QUEUE_BYTES': str(args.admission_queue_bytes),
                 'IMQUIC_RELAY_CONGESTION_OPTIONS': 'pacing_interval=1/1000,sudden_ce_threshold=off' if args.transport_mode == 'prague' else '',
                 'IMQUIC_UDP_SEND_BUFFER_BYTES': '1048576', 'IMQUIC_PACKET_TRAIN_MODE': '0'})
        time.sleep(.3)
        publisher_command = [str(PUBLISHER), server.IP(), '4443', str(args.manifest),
            str(args.output / 'publisher-ready.json')]
        if args.persistent:
            publisher_command = [sys.executable, str(ROOT / 'tools/l4s/priority_publisher_supervisor.py'),
                '--ready-file', str(args.output / 'publisher-ready.json'), '--', *publisher_command]
        publisher = launch(server, 'publisher', publisher_command)
        wait_path(args.output / 'publisher-ready.json', [relay, publisher], 180)
        bridge = launch(client, 'bridge', [NODE, str(BRIDGE), '--native', str(SIDECAR), '--relay-host',
            server.IP(), '--relay-port', '4443', '--listen', '8787', '--transport-mode', args.transport_mode,
            '--origin', '', '--origin', args.origin] + (['--persistent'] if args.persistent else []) +
            (['--congestion-options', 'pacing_interval=1/1000,sudden_ce_threshold=1/1'] if args.transport_mode == 'prague' else []),
            env={**os.environ, 'IMQUIC_UDP_RECEIVE_BUFFER_BYTES': '1048576',
                 'IMQUIC_STREAM_RECEIVE_WINDOW_BYTES': '1048576'})
        proxy = launch(None, 'proxy', [sys.executable, str(ROOT / 'tools/l4s/tcp_forward.py'),
            '--listen', '0.0.0.0:7790', '--target', '127.0.0.1:8787', '--network-pid', str(client.pid)])
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline and bridge.poll() is None:
            if sampler:
                sampler.check()
            if any(process.poll() is not None for process, _ in background):
                raise RuntimeError('background iperf exited before browser completion')
            if any(process.poll() is not None for process in (relay, publisher, proxy)):
                raise RuntimeError('MoQ service exited before browser completion')
            time.sleep(.2)
        if bridge.poll() is None: raise TimeoutError('browser did not close its MoQ audit before deadline')
        if bridge.returncode: raise RuntimeError(f'bridge failed: {bridge.returncode}')
        background_connections = {}
        if hasattr(topology, 'assert_background'):
            background_connections = topology.assert_background(devices)
        if any(process.poll() is not None for process, _ in background):
            raise RuntimeError('background iperf exited before final evidence')
        if sampler:
            sampler.stop()
            sampler.check()
        (args.output / 'guest-summary.json').write_text(json.dumps({'status': 'PASS',
            'controller': args.transport_mode, 'audit_closed': True,
            'background_active_at_completion': bool(background),
            'background_connections_at_completion': background_connections,
            'completion_unix_ns': time.time_ns(),
            'completion_monotonic_ns': time.monotonic_ns()}, indent=2) + '\n')
    except Exception as error:
        (args.output / 'failure.txt').write_text(f'{type(error).__name__}: {error}\n')
        raise
    finally:
        try:
            if sampler:
                sampler.stop()
            if processes: evidence('after')
        finally:
            try:
                for process in reversed(processes): stop(process)
            finally:
                for handle in handles: handle.close()
                net.stop()


if __name__ == '__main__':
    main()
