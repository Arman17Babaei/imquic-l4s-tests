"""Two-switch network with the same DualPI2 queues for both controllers.

CAMERA_PLAYER_MODE selects the transport controller, not the queue layout.
Both switches use target 15ms/update 16ms/step 1ms. The provider is 300Mbit/s.
Downstream defaults to 100 Mbit/s; the optional 20mbit treatment preserves the 128k HTB burst
and cburst so the explicit changed parameter is the downstream rate alone.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ENDPOINT_ROLES = ('server_egress', 'client_egress', 'background_sink_egress',
                  'background_source_egress', 'background_sink2_egress')
METRIC_ROLES = ('provider_egress', 'downstream_egress', 'server_egress', 'client_egress')


def build_topology():
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    server = net.addHost('server', ip='10.0.0.1/24')
    client = net.addHost('client', ip='10.0.0.2/24')
    sink = net.addHost('bg_sink', ip='10.0.0.3/24')
    source = net.addHost('bg_src', ip='10.0.0.4/24')
    sink2 = net.addHost('bg_sink2', ip='10.0.0.5/24')
    provider = net.addSwitch('s1', failMode='standalone')
    downstream = net.addSwitch('s2', failMode='standalone')
    devices = {}
    for first, second, role1, role2 in (
        (server, provider, 'server_egress', 'provider_ingress'),
        (provider, downstream, 'provider_egress', 'downstream_ingress'),
        (downstream, client, 'downstream_egress', 'client_egress'),
        (downstream, sink, 'downstream_to_sink', 'background_sink_egress'),
        (source, provider, 'background_source_egress', 'provider_to_source'),
        (sink2, downstream, 'background_sink2_egress', 'downstream_to_sink2'),
    ):
        link = net.addLink(first, second)
        devices[role1] = (first, link.intf1.name)
        devices[role2] = (second, link.intf2.name)
    return net, client, server, devices


def shaping_commands(device: str, role: str, mode: str, rate: str | None = None):
    if rate not in (None, '20mbit', '100mbit'):
        raise ValueError('downstream rate must be 20mbit or 100mbit')
    if mode not in ('reno', 'prague'):
        raise ValueError('CAMERA_PLAYER_MODE must be reno or prague')
    if role in ENDPOINT_ROLES:
        return [['tc', 'qdisc', 'replace', 'dev', device, 'root', 'netem', 'delay', '10ms', 'limit', '100000']]
    if role not in ('provider_egress', 'downstream_egress'):
        return []
    provider = role == 'provider_egress'
    rate, burst = ('300mbit', '512k') if provider else (rate or '100mbit', '128k')
    queue = ['dualpi2', 'target', '15ms', 'tupdate', '16ms', 'step_thresh', '1ms']
    handle = '10:'
    return [
        ['tc', 'qdisc', 'replace', 'dev', device, 'root', 'handle', '1:', 'htb', 'default', '1'],
        ['tc', 'class', 'replace', 'dev', device, 'parent', '1:', 'classid', '1:1',
         'htb', 'rate', rate, 'burst', burst, 'cburst', burst],
        ['tc', 'qdisc', 'replace', 'dev', device, 'parent', '1:1', 'handle', handle, *queue],
    ]


def checked(node, command):
    process = node.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output, _ = process.communicate(timeout=10)
    if process.returncode:
        raise RuntimeError(f'{command}: {output}')
    return output


def configure_topology(devices, rate: str | None = None, *, command_factory=None):
    if rate not in (None, '20mbit', '100mbit'):
        raise ValueError('downstream rate must be 20mbit or 100mbit')
    mode = os.environ.get('CAMERA_PLAYER_MODE', '')
    commands_for = command_factory or shaping_commands
    subprocess.run(['modprobe', 'sch_dualpi2'], check=True)
    subprocess.run(['modprobe', 'tcp_cubic'], check=True)
    for role, (node, device) in devices.items():
        checked(node, ['ethtool', '-K', device, 'gro', 'off', 'gso', 'off', 'tso', 'off'])
        for command in commands_for(device, role, mode, rate):
            checked(node, command)
    for role in ENDPOINT_ROLES:
        node, _ = devices[role]
        checked(node, ['sysctl', '-w', 'net.ipv4.tcp_ecn=0'])


def background_command(destination: str, port: int):
    # Unlimited, independent single TCP flows. Text intervals flush as they run,
    # including on guest versions predating iperf3 --json-stream.
    return ['iperf3', '-c', destination, '-p', str(port), '-C', 'cubic',
            '-t', '86400', '-i', '0.1', '--forceflush']


def start_background(devices, output: Path):
    processes = []
    try:
        for source_role, sink_role, port in (
            ('server_egress', 'background_sink_egress', 5201),
            ('background_source_egress', 'background_sink2_egress', 5202),
        ):
            source, _ = devices[source_role]
            sink, _ = devices[sink_role]
            for node, name, command in (
                (sink, f'background-{port}-server', ['iperf3', '-s', '-1', '-p', str(port), '-i', '0.1', '--forceflush']),
                (source, f'background-{port}-client', background_command(sink.IP(), port)),
            ):
                if name.endswith('client'):
                    time.sleep(.2)
                handle = (output / f'{name}.log').open('wb')
                try:
                    process = node.popen(command, stdout=handle, stderr=subprocess.STDOUT)
                except BaseException:
                    handle.close()
                    raise
                processes.append((process, handle))
        time.sleep(2)
        connections = assert_background(devices)
        if any(process.poll() is not None for process, _ in processes):
            raise RuntimeError('background iperf exited during warmup')
        (output / 'background-config.json').write_text(json.dumps({
            'cc': 'cubic', 'ecn': False, 'rate': 'unlimited', 'warmup_seconds': 2,
            'ports': [5201, 5202], 'ready_unix_ns': time.time_ns(),
            'ready_monotonic_ns': time.monotonic_ns(),
            'established_connections': connections}, indent=2) + '\n')
        return processes
    except BaseException:
        for process, handle in reversed(processes):
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            handle.close()
        raise


def assert_background(devices):
    connections = {}
    for role, port in (('server_egress', 5201), ('background_source_egress', 5202)):
        source, _ = devices[role]
        state = checked(source, ['ss', '-Htn', 'state', 'established', 'dport', '=', str(port)])
        if not state.strip():
            raise RuntimeError(f'background flow {port} has no established TCP connection')
        connections[str(port)] = state.strip()
    return connections
