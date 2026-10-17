#!/usr/bin/env python3
"""Capture a fixed full-scene player frame over HTTP, loopback MoQ, or QEMU MoQ."""
from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from experiment_metadata import write_experiment_record
from run_camera_trace_player import (ROOT, PLAYER, browser_command, free_port, sha256, terminate,
                                    validate_manifest, validate_trace, wait_file, wait_port)
from run_qemu_camera_priority import scene_archive


def run(args) -> int:
    interactive = getattr(args, 'interactive', False)
    if interactive:
        def stop_interactive(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop_interactive)
    if interactive and args.fixed_frame: raise ValueError('interactive playback cannot use --fixed-frame')
    if min(args.width, args.height, args.timeout_seconds) <= 0 or args.admission_delay_us < 0:
        raise ValueError('dimensions/timeout must be positive and admission delay nonnegative')
    if getattr(args, 'camera_check', False) and not args.fixed_frame:
        raise ValueError('--camera-check requires --fixed-frame for a completely loaded scene')
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'results').resolve()) or output.exists():
        raise ValueError('output must be a new directory under results/')
    manifest_path, trace_path = args.manifest.resolve(), args.trace.resolve()
    manifest, trace = validate_manifest(manifest_path), validate_trace(trace_path)
    if getattr(args, 'camera_check', False) and any('view_matrix' not in frame for frame in trace):
        raise ValueError('--camera-check requires the raw recorded source trace with view_matrix')
    if args.network_path == 'qemu' and args.transport != 'moqt':
        raise ValueError('QEMU path requires MoQ transport')
    output.mkdir(parents=True)
    # Each run serves its own build snapshot; rebuilding the player during QEMU
    # provisioning must not invalidate the page's content-hashed asset URLs.
    (output / 'player-ui').mkdir()
    shutil.copy2(PLAYER / 'dist/index.html', output / 'player-ui/index.html')
    shutil.copytree(PLAYER / 'dist/assets', output / 'player-ui/assets')
    processes, handles = [], []
    qemu = None
    ports = {'web': free_port(socket.SOCK_STREAM), 'bridge': free_port(socket.SOCK_STREAM),
             'relay': free_port(socket.SOCK_DGRAM)}
    def start(name, command, env=None):
        handle = (output / f'{name}.log').open('wb'); handles.append(handle)
        process = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, env=env)
        processes.append(process)
        return process
    browser_result = 1
    failure = None
    with tempfile.TemporaryDirectory(prefix='camera-player-input-') as temp:
        temporary = Path(temp)
        try:
            web_command = [sys.executable, str(ROOT / 'tools/l4s/camera_trace_player_server.py'),
                '--dist', str(output / 'player-ui'), '--manifest', str(manifest_path), '--trace', str(trace_path),
                '--port', str(ports['web'])]
            if args.transport == 'moqt': web_command.append('--metadata-only')
            web = start('web', web_command)
            wait_port(ports['web'], [web])
            if args.transport == 'moqt' and args.network_path == 'loopback':
                relay = start('relay', [str(ROOT / 'deps/imquic/examples/imquic-moq-relay'), '-M', '19', '-q',
                    '-b', '127.0.0.1', '-p', str(ports['relay']), '-c', str(ROOT / 'deps/picoquic/certs/cert.pem'),
                    '-k', str(ROOT / 'deps/picoquic/certs/key.pem'), '-Z', '-T', '-d', '4', '--congestion-control',
                    args.transport_mode, '--transport-metrics', str(output / 'transport-metrics.csv')],
                    env={**os.environ, 'IMQUIC_RELAY_ADMISSION_DELAY_US': str(args.admission_delay_us)})
                time.sleep(.3)
                publisher = start('publisher', [str(ROOT / 'build/priority-moq-publisher'), '127.0.0.1',
                    str(ports['relay']), str(manifest_path), str(output / 'publisher-ready.json')])
                wait_file(output / 'publisher-ready.json', [relay, publisher], timeout=180)
                bridge = start('bridge', ['node', str(ROOT / 'tools/l4s/priority_ws_bridge.mjs'), '--native',
                    str(ROOT / 'build/priority-moq-sidecar'), '--relay-host', '127.0.0.1', '--relay-port',
                    str(ports['relay']), '--listen', str(ports['bridge']), '--transport-mode', args.transport_mode,
                    '--origin', f"http://127.0.0.1:{ports['web']}"] + (['--persistent'] if interactive else []))
                wait_port(ports['bridge'], [relay, publisher, bridge])
            elif args.transport == 'moqt':
                archive = temporary / 'scene.tar'
                scene_archive(manifest_path, archive)
                node = shutil.which('node')
                if not node: raise RuntimeError('Node.js is required')
                prefix = f'camera-player-{output.name}'
                command = [sys.executable, str(ROOT / 'tools/l4s/run_qemu_timeseries_test.py'),
                    '--base', str(args.qemu_base.expanduser().resolve()), '--make-target', 'l4s-camera-player-guest-check',
                    '--guest-result-name', 'player', '--guest-result-root', 'results/l4s', '--destination-root', 'results/l4s',
                    '--destination-prefix', prefix, '--allow-dirty', '--memory', str(getattr(args, 'qemu_memory', 4096)),
                    '--cpus', str(getattr(args, 'qemu_cpus', 4)),
                    '--guest-timeout', str(int(args.timeout_seconds + 900)), '--forward-sidecar-port', str(ports['bridge']),
                    '--guest-file', f'{archive}=inputs/player-scene.tar', '--guest-file', f'{node}=inputs/node',
                    '--make-variable', 'CAMERA_PRIORITY_SCENE_ARCHIVE=inputs/player-scene.tar', '--make-variable',
                    'CAMERA_PRIORITY_NODE=inputs/node', '--make-variable', f'CAMERA_PLAYER_MODE={args.transport_mode}',
                    '--make-variable', f'CAMERA_PLAYER_DURATION={args.timeout_seconds}', '--make-variable',
                    f"CAMERA_PLAYER_ORIGIN=http://127.0.0.1:{ports['web']}", '--make-variable',
                    f'CAMERA_PLAYER_ADMISSION_DELAY_US={args.admission_delay_us}', '--make-variable',
                    f'CAMERA_PLAYER_BOTTLENECK={args.bottleneck}', '--make-variable',
                    f'CAMERA_PLAYER_PERSISTENT={1 if interactive else 0}']
                qemu = start('qemu', command)
                # SLIRP opens the host port before provisioning is complete. Require
                # a successful forwarded HTTP request, not merely an open TCP port.
                import urllib.request
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    if qemu.poll() is not None: raise RuntimeError('QEMU exited before bridge readiness; see qemu.log')
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{ports['bridge']}/audit", timeout=1) as response:
                            if response.status in (200, 202): break
                    except (OSError, ValueError): pass
                    time.sleep(.5)
                else: raise TimeoutError('QEMU bridge did not become ready')
            params = {'transport': args.transport, 'trace': '/camera-trace.json', 'fixedFrame': '1' if args.fixed_frame else '0',
                'autoplay': '0' if args.fixed_frame else '1', 'overlays': '0', 'dye': '0', 'record': '0',
                'sidecar': f"ws://127.0.0.1:{ports['bridge']}", 'nonce': str(time.time_ns())}
            if interactive: params.update({'play': '1', 'autoplay': '0', 'overlays': '1', 'dye': '1'})
            url = f"http://127.0.0.1:{ports['web']}/?{urlencode(params)}"
            write_experiment_record(output, scenario='camera-player-validation',
                configuration={'manifest': str(manifest_path), 'manifest_sha256': sha256(manifest_path),
                    'source_sha256': manifest['scene']['source_sha256'], 'gaussian_count': manifest['scene']['gaussian_count'],
                    'trace': str(trace_path), 'trace_sha256': sha256(trace_path), 'fixed_pose': trace[0],
                    'width': args.width, 'height': args.height, 'transport': args.transport, 'network_path': args.network_path,
                    'controller': args.transport_mode, 'payload_encoding': manifest.get('payload_encoding', 'splat'),
                    'background': 'black', 'overlays': interactive, 'dye': interactive,
                    'play_required': interactive, 'interactive': interactive, 'threshold_ssim': .90, 'ports': ports,
                    'admission_delay_us': args.admission_delay_us, 'bottleneck': args.bottleneck},
                topology='host browser -> forwarded WebSocket -> guest client IMQUIC -> DualPI2 -> relay/publisher'
                    if args.network_path == 'qemu' else 'browser -> HTTP or loopback WebSocket/IMQUIC -> relay/publisher',
                observed_network={'launcher_processes': [process.pid for process in processes]})
            if interactive:
                (output / 'endpoint.json').write_text(json.dumps({'url': url, 'ports': ports,
                    'bottleneck': args.bottleneck, 'controller': args.transport_mode,
                    'play_required': True, 'dye': True, 'pid': os.getpid()}, indent=2) + '\n')
                print(f'Interactive player ready: {url}', flush=True)
                while all(process.poll() is None for process in processes): time.sleep(.5)
                raise RuntimeError('Interactive service exited; see service logs')
            command = browser_command(url, output / 'unused.webm', output / 'player-telemetry.json',
                timeout_ms=int(args.timeout_seconds * 1000), width=args.width, height=args.height,
                headless=args.headless, record=False)
            if args.fixed_frame: command += ['--snapshot', str(output / 'browser.png')]
            if getattr(args, 'camera_check', False):
                from generate_camera_priority_trace import generate_trace
                yaw_path = output / 'yaw-trace.json'
                yaw_frames = generate_trace(trace)
                for frame in yaw_frames: frame['aspect_ratio'] = args.width / args.height
                yaw_path.write_text(json.dumps(yaw_frames, indent=2) + '\n')
                command += ['--motion-trace', str(trace_path), '--yaw-trace', str(yaw_path)]
            browser = start('browser', command)
            browser_result = browser.wait(timeout=args.timeout_seconds + 45)
            if args.network_path == 'qemu':
                if qemu.wait(timeout=90): raise RuntimeError('QEMU validation failed after browser completion')
                candidates = list((ROOT / 'results/l4s').glob(f'{prefix}-*'))
                (output / 'guest-evidence.json').write_text(json.dumps({'directories': [str(path) for path in candidates]}, indent=2) + '\n')
        except KeyboardInterrupt:
            failure = 'Interactive endpoint stopped'
        except Exception as error:
            failure = f'{type(error).__name__}: {error}'
            (output / 'failure.txt').write_text(failure + '\n')
        finally:
            if interactive and qemu is not None and qemu.poll() is None:
                qemu.terminate()
                try: qemu.wait(timeout=75)
                except subprocess.TimeoutExpired: pass
            exits = terminate(processes)
            for handle in handles: handle.close()
    telemetry_path = output / 'player-telemetry.json'
    telemetry = json.loads(telemetry_path.read_text()) if telemetry_path.exists() else {}
    complete = telemetry.get('receivedGaussians') == manifest['scene']['gaussian_count'] and telemetry.get('appliedGaussians') == manifest['scene']['gaussian_count']
    success = browser_result == 0 and failure is None and complete and telemetry.get('rejected') == 0
    summary = {'status': 'PASS' if success else 'FAIL', 'failure': failure, 'browser_exit_code': browser_result,
        'complete_scene': complete, 'transport': args.transport, 'network_path': args.network_path,
        'controller': args.transport_mode, 'process_exit_codes': exits,
        'rendering': 'NOT_EVALUATED: run compare_player_frame.py against exact reference'}
    (output / 'validation-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({key: value for key, value in summary.items() if key != 'process_exit_codes'}))
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--transport', choices=('http', 'moqt'), default='moqt')
    parser.add_argument('--network-path', choices=('loopback', 'qemu'), default='loopback')
    parser.add_argument('--transport-mode', choices=('prague', 'reno'), default='reno')
    parser.add_argument('--qemu-memory', type=int, default=4096, help='Guest RAM in MiB')
    parser.add_argument('--qemu-cpus', type=int, default=4, help='Guest vCPUs and parallel build jobs')
    parser.add_argument('--qemu-base', type=Path, default=Path('~/sandbox/p4/work.qcow2'))
    parser.add_argument('--fixed-frame', action='store_true')
    parser.add_argument('--camera-check', action='store_true', help='capture recorded poses and 50-second yaw knots after loading')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--interactive', action='store_true', help='serve a persistent endpoint with Play gating and dye; do not launch an automated client')
    parser.add_argument('--timeout-seconds', type=float, default=600)
    parser.add_argument('--admission-delay-us', type=int, default=0)
    parser.add_argument('--bottleneck', choices=('10mbit', '20mbit', '50mbit', '100mbit'), default='20mbit')
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=540)
    args = parser.parse_args()
    if min(args.qemu_memory, args.qemu_cpus) <= 0: parser.error('QEMU memory and CPUs must be positive')
    if min(args.timeout_seconds, args.width, args.height) <= 0: parser.error('timeout and dimensions must be positive')
    raise SystemExit(run(args))


if __name__ == '__main__':
    main()
