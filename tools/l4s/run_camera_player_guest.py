#!/usr/bin/env python3
"""Expose the priority MoQ bridge to a host browser through QEMU forwarding."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from experiment_metadata import capture_mininet_state, write_experiment_record
from run_camera_priority_guest import (BRIDGE, NODE, PUBLISHER, RELAY, SIDECAR, ROOT,
                                      configure_device, counters, start, stop, wait_path, write_tc)


def main():
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--transport-mode', choices=('prague', 'reno'), required=True)
    parser.add_argument('--duration', type=float, default=600)
    parser.add_argument('--origin', default='')
    parser.add_argument('--admission-delay-us', type=int, default=0)
    parser.add_argument('--bottleneck', choices=('10mbit', '20mbit', '50mbit', '100mbit'), default='20mbit')
    parser.add_argument('--persistent', action='store_true')
    parser.add_argument('--admission-queue-bytes', type=int, default=262144)
    args = parser.parse_args()
    if not 65536 <= args.admission_queue_bytes <= 2097152:
        parser.error('--admission-queue-bytes must be in 65536..2097152')
    args.output.mkdir(parents=True, exist_ok=False)
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost('client', ip='10.0.0.1/24')
    switch = net.addSwitch('s1', failMode='standalone')
    server = net.addHost('server', ip='10.0.0.2/24')
    first, second = net.addLink(client, switch), net.addLink(switch, server)
    processes, handles = [], []
    devices = {'client_to_s1': (client, first.intf1.name), 's1_to_client': (None, first.intf2.name),
               's1_to_server': (None, second.intf1.name), 'server_to_s1': (server, second.intf2.name)}
    def launch(node, name, command, env=None):
        if node is None:
            handle = (args.output / f'{name}.log').open('wb')
            process = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env)
        else:
            process, handle = start(node, command, args.output / f'{name}.log', env=env)
        processes.append(process); handles.append(handle)
        return process
    def evidence(phase):
        write_tc(args.output, phase, devices)
        (args.output / f'interface-counters-{phase}.json').write_text(json.dumps({
            name: counters(node, device) for name, (node, device) in devices.items()}, indent=2) + '\n')
    try:
        net.start()
        subprocess.run(['modprobe', 'sch_dualpi2'], check=True)
        for node, device in devices.values():
            configure_device(node, device, args.bottleneck, player_propagation=node is None)
        evidence('before')
        write_experiment_record(args.output, scenario='camera-player-qemu',
            configuration={'manifest': str(args.manifest), 'controller': args.transport_mode,
                'duration_limit_seconds': args.duration, 'rate': args.bottleneck,
                'delay_switch_egress': '40ms', 'aqm_host_egress': 'HTB -> DualPI2',
                'dualpi2_step_thresh': '5ms',
                'admission_delay_us': args.admission_delay_us,
                'admission_queue_bytes': args.admission_queue_bytes,
                'relay_congestion_options': 'pacing_interval=1/1000,sudden_ce_threshold=off' if args.transport_mode == 'prague' else '',
                'subscriber_congestion_options': 'pacing_interval=1/1000,sudden_ce_threshold=1/1' if args.transport_mode == 'prague' else '',
                'udp_send_buffer_bytes': 1048576, 'udp_receive_buffer_bytes': 1048576,
                'stream_receive_window_bytes': 1048576, 'packet_train_mode': 0,
                'persistent_bridge': args.persistent, 'subscriber_start': 'browser Play' if args.persistent else 'immediate',
                'question': 'Does the host browser receive hashed SH3 MoQ objects through guest DualPI2?',
                'hypothesis': 'Browser-applied objects match the publisher and all data crosses shaped guest interfaces.',
                'control': 'Reno with the same scene and topology', 'capture_mode': 'transport-metrics'},
            topology={'path': 'host browser -> QEMU TCP forward -> guest proxy -> client bridge/subscriber -> s1 -> server relay/publisher',
                'moq_client': client.IP(), 'moq_server': server.IP(), 'relay_port': 4443, 'guest_forward_port': 7790},
            observed_network=capture_mininet_state(client, server, switch))
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
            if any(process.poll() is not None for process in (relay, publisher, proxy)):
                raise RuntimeError('MoQ service exited before browser completion')
            time.sleep(.2)
        if bridge.poll() is None: raise TimeoutError('browser did not close its MoQ audit before deadline')
        if bridge.returncode: raise RuntimeError(f'bridge failed: {bridge.returncode}')
        (args.output / 'guest-summary.json').write_text(json.dumps({'status': 'PASS',
            'controller': args.transport_mode, 'audit_closed': True}, indent=2) + '\n')
    except Exception as error:
        (args.output / 'failure.txt').write_text(f'{type(error).__name__}: {error}\n')
        raise
    finally:
        for process in reversed(processes): stop(process)
        if processes: evidence('after')
        for handle in handles: handle.close()
        net.stop()


if __name__ == '__main__':
    main()
