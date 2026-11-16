#!/usr/bin/env python3
"""Render a complete scene on the camera trace's independent, half-open frame grid."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def input_provenance(manifest: Path, trace: Path) -> dict:
    document = json.loads(manifest.read_text())
    assets = []
    for name in sorted({refinement['file'] for tile in document.get('tiles', [])
                        for refinement in tile.get('refinements', [])}):
        asset = (manifest.parent / name).resolve()
        if Path(name).is_absolute() or not asset.is_relative_to(manifest.parent.resolve()):
            raise ValueError(f'asset escapes manifest directory: {name}')
        assets.append({'file': name, 'bytes': asset.stat().st_size, 'sha256': sha256(asset)})
    return {'manifest': {'path': str(manifest), 'sha256': sha256(manifest)},
            'trace': {'path': str(trace), 'sha256': sha256(trace)}, 'assets': assets}


def validate_output(output: Path) -> Path:
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'results'):
        raise ValueError('reference output must be under repository results/')
    if output.exists():
        raise FileExistsError(f'reference output already exists: {output}')
    return output


def snapshot_player_ui(dist: Path, target: Path) -> None:
    # The server's dist scan also registers JSON files. A bundled scene manifest
    # would replace the explicitly selected manifest route, so copy only the UI.
    target.mkdir()
    shutil.copy2(dist / 'index.html', target / 'index.html')
    shutil.copytree(dist / 'assets', target / 'assets')


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dist', type=Path, default=ROOT / 'deps/gaussian-player/dist')
    parser.add_argument('--fps', type=int, default=10)
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=540)
    parser.add_argument('--timeout-seconds', type=float, default=600)
    parser.add_argument('--gpu-mode', choices=('hardware', 'software'), default='hardware')
    parser.add_argument('--headless', action='store_true')
    args = parser.parse_args(argv)
    if min(args.fps, args.width, args.height, args.timeout_seconds) <= 0:
        parser.error('FPS, dimensions and timeout must be positive')
    manifest, trace, dist = args.manifest.resolve(), args.trace.resolve(), args.dist.resolve()
    output = validate_output(args.output)
    evidence = input_provenance(manifest, trace)
    if not (dist / 'index.html').is_file():
        raise FileNotFoundError(f'build the player first: {dist / "index.html"}')
    evidence['playerAssets'] = [{'file': str(asset.relative_to(dist)), 'sha256': sha256(asset)}
        for asset in sorted(dist.rglob('*')) if asset.is_file()
        and asset.suffix not in ('.splat', '.ply', '.gsp2')]
    # Import before creating outputs so a missing encoder does not leave a misleading run directory.
    from snapshot_video import encode_snapshot_video
    output.mkdir(parents=True)
    player_ui = output / 'player-ui'
    snapshot_player_ui(dist, player_ui)
    evidence.update({'complete': False, 'requestedFps': args.fps, 'viewport': [args.width, args.height],
                     'mode': 'independent-complete-scene-trace-grid'})
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    server_command = [sys.executable, str(ROOT / 'tools/l4s/camera_trace_player_server.py'),
        '--dist', str(player_ui), '--manifest', str(manifest), '--trace', str(trace), '--port', str(port)]
    params = {'transport': 'http', 'trace': '/camera-trace.json', 'fixedFrame': '1',
              'autoplay': '0', 'overlays': '0', 'dye': '0', 'record': '0'}
    url = f'http://127.0.0.1:{port}/?{urlencode(params)}'
    browser_command = ['node', str(ROOT / 'tools/l4s/camera_trace_browser_runner.mjs'),
        '--url', url, '--telemetry', str(output / 'reference-telemetry.json'), '--timeout-ms',
        str(int(args.timeout_seconds * 1000)), '--width', str(args.width), '--height', str(args.height),
        '--gpu-mode', args.gpu_mode, '--no-record', '--reference-trace-grid', '--motion-trace', str(trace),
        '--capture-frames', str(output / 'reference-frames'), '--capture-fps', str(args.fps)]
    if args.headless:
        browser_command.append('--headless')
    evidence['commands'] = {'server': server_command, 'browser': browser_command}
    server = None
    try:
        with (output / 'reference-server.log').open('w') as server_log:
            server = subprocess.Popen(server_command, stdout=server_log, stderr=subprocess.STDOUT, cwd=ROOT)
            deadline = time.monotonic() + 15
            while True:
                if server.poll() is not None:
                    raise RuntimeError('reference server exited; see reference-server.log')
                try:
                    with urlopen(f'http://127.0.0.1:{port}/', timeout=1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('reference server did not start')
                    time.sleep(0.1)
            with (output / 'reference-browser.log').open('w') as browser_log:
                subprocess.run(browser_command, check=True, cwd=ROOT, stdout=browser_log,
                               stderr=subprocess.STDOUT, timeout=args.timeout_seconds + 60)
        telemetry = json.loads((output / 'reference-telemetry.json').read_text())
        evidence['browserContext'] = telemetry.get('browserContext')
        evidence['browserVersion'] = telemetry.get('browserVersion')
        expected_gaussians = json.loads(manifest.read_text())['scene']['gaussian_count']
        if telemetry.get('failure') or telemetry.get('appliedGaussians') != expected_gaussians:
            raise RuntimeError('reference did not render the complete scene; inspect telemetry')
        evidence['appliedGaussians'] = telemetry['appliedGaussians']
        evidence['video'] = encode_snapshot_video(output / 'reference-frames/frames.json',
                                                  output / 'reference.mp4', fps=args.fps)
        evidence['complete'] = True
    except Exception as error:
        evidence['failure'] = str(error)
        raise
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        telemetry_path = output / 'reference-telemetry.json'
        if telemetry_path.exists():
            evidence['browserContext'] = json.loads(telemetry_path.read_text()).get('browserContext')
        (output / 'reference-summary.json').write_text(json.dumps(evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
