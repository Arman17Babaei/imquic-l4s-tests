"""One-switch diagnostic: all five links are20Mbps in both directions.

Use IFB ingress delay to avoid nesting the dropping AQM beneath netem.
"""
import os
import json
import time
from pathlib import Path
import subprocess
import thesis_player_topology as base

ENDPOINT_ROLES = base.ENDPOINT_ROLES
METRIC_ROLES = ('provider_egress', 'downstream_egress', *ENDPOINT_ROLES)
assert_background = base.assert_background
SWITCH_ROLES = ('provider_egress', 'downstream_egress', 'switch_to_background_sink',
                'switch_to_background_source', 'switch_to_background_sink2')


def build_topology():
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge
    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    switch = net.addSwitch('s1', failMode='standalone')
    devices = {}
    hosts = {}
    for name, address, host_role, switch_role in zip(
        ('server', 'client', 'bg_sink', 'bg_src', 'bg_sink2'),
        range(1, 6), ENDPOINT_ROLES, SWITCH_ROLES):
        host = net.addHost(name, ip=f'10.0.0.{address}/24')
        hosts[name] = host
        link = net.addLink(host, switch)
        devices[host_role] = (host, link.intf1.name)
        devices[switch_role] = (switch, link.intf2.name)
        devices[host_role + '_propagation'] = (host, 'ifb0')
    return net, hosts['client'], hosts['server'], devices


def configure_topology(devices, rate=None):
    if rate not in (None, '20mbit'):
        raise ValueError('all links must be20mbit')
    if os.environ.get('CAMERA_PLAYER_MODE') not in ('reno', 'prague'):
        raise ValueError('unknown controller')
    for module in ('sch_dualpi2', 'tcp_cubic', 'ifb'):
        subprocess.run(['modprobe', module], check=True)
    for role in (*ENDPOINT_ROLES, *SWITCH_ROLES):
        node, interface = devices[role]
        base.checked(node, ['ethtool', '-K', interface, 'gro', 'off', 'gso', 'off', 'tso', 'off'])
        for command in (
            ['tc', 'qdisc', 'replace', 'dev', interface, 'root', 'handle', '1:', 'htb', 'default', '1'],
            ['tc', 'class', 'replace', 'dev', interface, 'parent', '1:', 'classid', '1:1',
             'htb', 'rate', '20mbit', 'burst', '32k'],
            ['tc', 'qdisc', 'replace', 'dev', interface, 'parent', '1:1', 'handle', '10:',
             'dualpi2', 'target', '15ms', 'tupdate', '16ms', 'step_thresh', '5ms', 'coupling_factor', '1'],
        ):
            base.checked(node, command)
    for role in ENDPOINT_ROLES:
        node, interface = devices[role]
        for command in (
            ['sysctl', '-w', 'net.ipv4.tcp_ecn=0'],
            ['ip', 'link', 'add', 'ifb0', 'type', 'ifb'],
            ['ip', 'link', 'set', 'ifb0', 'up'],
            ['ethtool', '-K', 'ifb0', 'gro', 'off', 'gso', 'off', 'tso', 'off'],
            ['tc', 'qdisc', 'replace', 'dev', 'ifb0', 'root', 'netem', 'delay', '10ms', 'limit', '100000'],
            ['tc', 'qdisc', 'replace', 'dev', interface, 'handle', 'ffff:', 'ingress'],
            ['tc', 'filter', 'replace', 'dev', interface, 'parent', 'ffff:', 'protocol', 'all',
             'pref', '10', 'matchall', 'action', 'mirred', 'egress', 'redirect', 'dev', 'ifb0'],
        ):
            base.checked(node, command)


def start_background(devices, output: Path):
    processes = []
    manifest=[]
    try:
        for source_role, sink_role, port in (
            ('server_egress', 'background_sink_egress', 5201),
            ('background_source_egress', 'background_sink2_egress', 5202),
        ):
            source, _ = devices[source_role]
            sink, _ = devices[sink_role]
            for node, name, command in (
                (sink, f'background-{port}-server', ['iperf3', '-s', '-1', '-p', str(port), '-i', '0.1', '--forceflush']),
                (source, f'background-{port}-client', base.background_command(sink.IP(), port)),
            ):
                if name.endswith('client'):
                    time.sleep(.2)
                handle = (output / f'{name}.log').open('wb')
                try:
                    cpu=({5201:0,5202:4}[port] if name.endswith('client') else {5201:5,5202:6}[port])
                    process = node.popen(['nice','-n','-10','taskset','-c',str(cpu),*command], stdout=handle, stderr=subprocess.STDOUT)
                    manifest.append(dict(pid=process.pid,cpu=cpu,name=name,command=command))
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
        (output/'background-processes.json').write_text(json.dumps(manifest,indent=2)+'\n')
        return processes
    except BaseException:
        for process, handle in reversed(processes):
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            handle.close()
        raise
