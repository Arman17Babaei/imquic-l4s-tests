"""Two-switch scene testbed with unlimited Cubic and Prague TCP competition.

Edit these constants for direct runs. The experiment suite copies this source
and overrides the same constants, preserving a standalone guest topology.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time

BOTTLENECK = '50mbit'
PROPAGATION_DELAY_MS = 40
DUALPI2_TARGET_MS = 15
DUALPI2_UPDATE_MS = 16
DUALPI2_STEP_MS = 1
DUALPI2_COUPLING_FACTOR = 1
BACKGROUND_WARMUP_SECONDS = 4
PERFORMANCE_MODE = False
OUTPUT = None


def background_pairs():
    return [dict(index=i, cc=cc, tcp_ecn=3 if cc == 'prague' else 0, source=f'{cc}src', sink=f'{cc}sink',
                 source_role=f'{cc}src_to_s1', sink_role=f'{cc}sink_to_s2',
                 destination=f'10.0.0.{2*i+2}', port=5200+i, shared_role='s1_to_s2')
            for i, cc in enumerate(('cubic', 'prague'), 1)]


METRIC_ROLES = ('s1_to_s2', 's2_to_s1', 'server_to_s1', 'client_to_s2',
                'cubicsrc_to_s1', 'cubicsink_to_s2',
                'praguesrc_to_s1', 'praguesink_to_s2')


def set_output(output):
    global OUTPUT
    OUTPUT = output


def build_topology():
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    server = net.addHost('server', ip='10.0.0.2/24')
    client = net.addHost('client', ip='10.0.0.1/24')
    s1 = net.addSwitch('s1', failMode='standalone')
    s2 = net.addSwitch('s2', failMode='standalone')
    links = [(server, s1), (s1, s2), (s2, client)]
    for pair in background_pairs():
        source = net.addHost(pair['source'], ip=f"10.0.0.{2*pair['index']+1}/24")
        sink = net.addHost(pair['sink'], ip=pair['destination']+'/24')
        links.extend(((source, s1), (s2, sink)))
    devices = {}
    for first, second in links:
        link = net.addLink(first, second)
        devices[f'{first.name}_to_{second.name}'] = (first, link.intf1.name)
        devices[f'{second.name}_to_{first.name}'] = (second, link.intf2.name)
    return net, client, server, devices


def shaping_commands(device: str, *, shared: bool, rate: str | None = None):
    commands = [
        ['tc', 'qdisc', 'replace', 'dev', device, 'root', 'handle', '10:', 'htb', 'default', '1'],
        ['tc', 'class', 'replace', 'dev', device, 'parent', '10:', 'classid', '10:1',
         'htb', 'rate', (rate or BOTTLENECK) if shared else '300mbit',
         'burst', '512k', 'cburst', '512k'],
    ]
    # Both access links contribute half the one-way propagation. Their netem
    # queues are physically separate from the inter-switch dropping AQM.
    leaf = (['dualpi2', 'coupling_factor', str(DUALPI2_COUPLING_FACTOR),
             'target', f'{DUALPI2_TARGET_MS:g}ms', 'tupdate', f'{DUALPI2_UPDATE_MS:g}ms',
             'step_thresh', f'{DUALPI2_STEP_MS:g}ms'] if shared else
            ['netem', 'delay', f'{PROPAGATION_DELAY_MS/2:g}ms', 'limit', '100000'])
    return commands + [['tc', 'qdisc', 'replace', 'dev', device, 'parent', '10:1',
                        'handle', '20:', *leaf]]


def run_command(node, command):
    process = node.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output, _ = process.communicate(timeout=10)
    if process.returncode:
        raise RuntimeError(f"{' '.join(command)}: {output}")
    return output


def configure_topology(devices, rate: str | None = None):
    for module in ('sch_dualpi2', 'tcp_cubic', 'tcp_prague'):
        subprocess.run(['modprobe', module], check=True)
    for role, (node, device) in devices.items():
        # Avoid batching bypassing queue accounting, for both scene and TCP.
        run_command(node, ['ethtool', '-K', device, 'gro', 'off', 'gso', 'off', 'tso', 'off'])
        for command in shaping_commands(device, shared=role in ('s1_to_s2', 's2_to_s1'), rate=rate):
            run_command(node, command)
    for pair in background_pairs():
        for role in (pair['source_role'], pair['sink_role']):
            node, _ = devices[role]
            # The experiment's Prague kernel uses 3 to request Accurate ECN.
            # Check negotiated socket state at startup rather than assuming it.
            run_command(node, ['sysctl', '-w', f"net.ipv4.tcp_ecn={pair['tcp_ecn']}"])
            run_command(node, ['sysctl', '-w', f"net.ipv4.tcp_congestion_control={pair['cc']}"])
    if PERFORMANCE_MODE:
        configure_performance()


def process_command(node, name, command):
    if not PERFORMANCE_MODE:
        return command
    cpu = {'publisher': 0, 'relay': 1, 'bridge': 3, 'proxy': 3}.get(name, 3)
    return ['nice', '-n', '-10', 'taskset', '-c', str(cpu), *command]


def configure_performance():
    for pid in subprocess.check_output(['pgrep', '-x', 'ovs-vswitchd|ovsdb-server'], text=True).split():
        for task in (Path('/proc')/pid/'task').iterdir():
            os.sched_setaffinity(int(task.name), {2})
            os.setpriority(os.PRIO_PROCESS, int(task.name), -10)
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            name = (proc/'comm').read_text().strip()
            if name.startswith('ksoftirqd/') and name != 'ksoftirqd/3':
                os.setpriority(os.PRIO_PROCESS, int(proc.name), -10)
        except (FileNotFoundError, ProcessLookupError):
            pass
    if OUTPUT:
        (OUTPUT/'performance-profile.json').write_text(json.dumps(dict(
            guest_cpus=7, ovs_cpu=2, nice=-10, offloads_disabled=['gro','gso','tso'],
            main_process_cpus=dict(publisher=0,relay=1,bridge=3,proxy=3),
            dualpi2_target_ms=DUALPI2_TARGET_MS, dualpi2_update_ms=DUALPI2_UPDATE_MS,
            dualpi2_step_ms=DUALPI2_STEP_MS, dualpi2_coupling_factor=DUALPI2_COUPLING_FACTOR))+'\n')
    if os.environ.get('CAMERA_PLAYER_MODE') == 'prague':
        os.environ.setdefault('CAMERA_PLAYER_RELAY_CONGESTION_OPTIONS',
                              'pacing_interval=1/4000,sudden_ce_threshold=off')
        os.environ.setdefault('CAMERA_PLAYER_SUBSCRIBER_CONGESTION_OPTIONS',
                              'pacing_interval=1/4000,sudden_ce_threshold=1/1')


def background_command(pair):
    return ['iperf3', '-4', '-c', pair['destination'], '-p', str(pair['port']),
            '-C', pair['cc'], '-t', '86400', '-i', '0.1', '--forceflush']


def assert_background(devices):
    connections = {}
    for pair in background_pairs():
        node, _ = devices[pair['source_role']]
        state = run_command(node, ['ss', '-Htin', 'state', 'established', 'dport', '=', str(pair['port'])])
        # iperf has a control socket and exactly one data socket. Inspect both;
        # the kernel may fall back from Prague when ECN negotiation fails.
        details = []
        lines = state.splitlines()
        for index, line in enumerate(lines):
            if re.search(r'\d+\.\d+\.\d+\.\d+:'+str(pair['port'])+r'\b', line):
                details.append(lines[index+1].strip() if index+1 < len(lines) else '')
        if len(details) != 2:
            raise RuntimeError(f"background {pair['cc']} lacks its single TCP data connection: {state}")
        if any(not re.match(rf"{pair['cc']}\b", detail) for detail in details):
            raise RuntimeError(f"background did not retain requested {pair['cc']} algorithm: {state}")
        if pair['cc'] == 'prague':
            def counter(detail, name):
                match = re.search(rf'\b{name}:(\d+)', detail)
                return int(match[1]) if match else 0
            data = max(details, key=lambda detail: counter(detail, 'bytes_sent'))
            # Older iproute2 omits the ECN option for this Accurate-ECN kernel.
            # Nonzero CE feedback is direct evidence that ECN is operating;
            # merely requesting tcp_ecn=3 or seeing a control socket is not.
            if not re.search(r'\becn\b', data) and counter(data, 'delivered_ce') == 0:
                raise RuntimeError(f'Prague background lacks negotiated ECN or observed CE feedback: {state}')
        connections[str(pair['port'])] = state.strip()
    return connections


def received_background_data(log):
    if not log.is_file():
        return False
    return any(float(value) > 0 for value in re.findall(
        r'\[\s*\d+\]\s+[\d.]+-\s*[\d.]+\s+sec\s+([\d.]+)\s+[KMGT]?Bytes\b',
        log.read_text(errors='replace')))


def start_background(devices, output: Path):
    processes, records = [], []
    config = dict(flow_count=2, rate='unlimited', pairs=background_pairs(),
                  one_way_delay_ms=PROPAGATION_DELAY_MS, warmup_seconds=BACKGROUND_WARMUP_SECONDS,
                  performance_mode=PERFORMANCE_MODE, status='STARTING')
    try:
        for server in (True, False):
            for pair in background_pairs():
                node, _ = devices[pair['sink_role'] if server else pair['source_role']]
                name = f"background-{pair['port']}-{'server' if server else 'client'}"
                command = (['iperf3', '-4', '-s', '-1', '-B', pair['destination'],
                            '-p', str(pair['port']), '-i', '0.1', '--forceflush']
                           if server else background_command(pair))
                if PERFORMANCE_MODE:
                    cpu = 6 if server else 3+pair['index']
                    command = ['nice', '-n', '-10', 'taskset', '-c', str(cpu), *command]
                handle = (output/f'{name}.log').open('wb')
                try:
                    process = node.popen(command, stdout=handle, stderr=subprocess.STDOUT)
                except BaseException:
                    handle.close()
                    raise
                processes.append((process, handle))
                records.append(dict(name=name, node=node.name, pid=process.pid, command=command))
            if server:
                time.sleep(.2)
        deadline = time.monotonic()+max(10, 6*PROPAGATION_DELAY_MS/1000+5)
        last_error = 'receiver has not logged any payload'
        while True:
            if any(p.poll() is not None for p, _ in processes):
                raise RuntimeError('background iperf exited during startup; see background logs')
            try:
                assert_background(devices)
                if all(received_background_data(output/f"background-{p['port']}-server.log")
                       for p in background_pairs()):
                    break
            except RuntimeError as error:
                last_error = str(error)
            if time.monotonic() >= deadline:
                raise RuntimeError(f'background data did not become ready: {last_error}; see background logs')
            time.sleep(.1)
        config.update(data_ready_unix_ns=time.time_ns(), data_ready_monotonic_ns=time.monotonic_ns())
        time.sleep(BACKGROUND_WARMUP_SECONDS)
        if any(p.poll() is not None for p, _ in processes):
            raise RuntimeError('background iperf exited during warmup; see background logs')
        config.update(status='PASS', established_connections=assert_background(devices),
                      ready_unix_ns=time.time_ns(), ready_monotonic_ns=time.monotonic_ns())
        return processes
    except BaseException as error:
        config.update(status='FAIL', error=f'{type(error).__name__}: {error}')
        (output/'background-failure.txt').write_text(config['error']+'\n')
        for process, handle in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            handle.close()
        raise
    finally:
        (output/'background-config.json').write_text(json.dumps(config, indent=2)+'\n')
        (output/'background-processes.json').write_text(json.dumps(records, indent=2)+'\n')
