#!/usr/bin/env python3
"""Render a complete scene on the camera trace's independent, half-open frame grid."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
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


def cached_trace_reference(*, cache_root: Path, manifest: Path, trace: Path, dist: Path,
                           fps: int, width: int, height: int, gpu_mode: str,
                           headless: bool, timeout_seconds: float) -> Path:
    """Publish a complete trace reference once; candidate captures never define it."""
    from compare_player_sequence import load_frames, snapshot_coverage

    cache_root = cache_root.resolve()
    if not cache_root.is_relative_to(ROOT / 'results'):
        raise ValueError('reference cache must be under repository results/')
    provenance = input_provenance(manifest, trace)
    # Input locations vary per attempt; only contents and render settings matter.
    identity = {'version': 1, 'manifest': provenance['manifest']['sha256'],
                'trace': provenance['trace']['sha256'], 'assets': provenance['assets'],
                'fps': fps, 'viewport': [width, height], 'gpu_mode': gpu_mode, 'headless': headless,
                'player': {str(path.relative_to(dist)): sha256(path)
                           for path in [dist / 'index.html', *sorted((dist / 'assets').rglob('*'))]
                           if path.is_file()},
                'renderer': {name: sha256(ROOT / 'tools/l4s' / name) for name in (
                    'render_trace_reference.py', 'camera_trace_browser_runner.mjs',
                    'player_quality.mjs', 'camera_trace_player_server.py')}}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache_root.mkdir(parents=True, exist_ok=True)
    entry = cache_root / key

    def validate(directory):
        summary = json.loads((directory / 'reference-summary.json').read_text())
        frames_path = directory / 'reference-frames/frames.json'
        frames = load_frames(frames_path)
        record = json.loads(frames_path.read_text())
        expected = math.ceil(json.loads(trace.read_text())[-1]['timestamp_ms'] * fps / 1000)
        if (not summary.get('complete') or not snapshot_coverage(record)['accepted'] or
                len(frames) != expected or
                summary.get('appliedGaussians') != json.loads(manifest.read_text())['scene']['gaussian_count'] or
                any(abs(frame['timestampMs'] - index * 1000 / fps) > 1e-6
                    for index, frame in enumerate(frames))):
            raise ValueError(f'incomplete trace reference cache: {directory}')
        for name in ('reference.mp4', 'reference-telemetry.json'):
            if not (directory / name).is_file():
                raise ValueError(f'missing cached reference artifact: {directory / name}')

    # Concurrent runners sharing a cache must never publish two renders of one key.
    with (cache_root / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if entry.exists():
            if json.loads((entry / 'cache-identity.json').read_text()) != identity:
                raise ValueError(f'reference cache identity mismatch: {entry}')
            validate(entry)
            print(f'REFERENCE cache hit: {entry}', flush=True)
            return entry
        # Failed staging directories retain logs, but cannot count as cache hits.
        staging = Path(tempfile.mkdtemp(prefix=key + '.pending-', dir=cache_root)) / 'render'
        command = ['--manifest', str(manifest), '--trace', str(trace), '--dist', str(dist),
                   '--output', str(staging), '--fps', str(fps), '--width', str(width),
                   '--height', str(height), '--gpu-mode', gpu_mode,
                   '--timeout-seconds', str(timeout_seconds)]
        if headless:
            command.append('--headless')
        print(f'REFERENCE render: {entry}', flush=True)
        main(command)
        validate(staging)
        (staging / 'cache-identity.json').write_text(json.dumps(identity, indent=2) + '\n')
        staging.rename(entry)
        staging.parent.rmdir()
        return entry


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
    started = time.perf_counter()
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
                browser_started = time.perf_counter()
                subprocess.run(browser_command, check=True, cwd=ROOT, stdout=browser_log,
                               stderr=subprocess.STDOUT, timeout=args.timeout_seconds + 60)
                evidence['render_seconds'] = time.perf_counter() - browser_started
        telemetry = json.loads((output / 'reference-telemetry.json').read_text())
        evidence['browserContext'] = telemetry.get('browserContext')
        evidence['browserVersion'] = telemetry.get('browserVersion')
        expected_gaussians = json.loads(manifest.read_text())['scene']['gaussian_count']
        if telemetry.get('failure') or telemetry.get('appliedGaussians') != expected_gaussians:
            raise RuntimeError('reference did not render the complete scene; inspect telemetry')
        evidence['appliedGaussians'] = telemetry['appliedGaussians']
        encode_started = time.perf_counter()
        evidence['video'] = encode_snapshot_video(output / 'reference-frames/frames.json',
                                                  output / 'reference.mp4', fps=args.fps)
        evidence['encode_seconds'] = time.perf_counter() - encode_started
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
        evidence['total_seconds'] = time.perf_counter() - started
        (output / 'reference-summary.json').write_text(json.dumps(evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
