#!/usr/bin/env python3
"""Launch camera-trace playback, recording, or validation over HTTP or MoQ."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from experiment_metadata import write_experiment_record

ROOT = Path(__file__).resolve().parents[2]
PLAYER = ROOT / "deps/gaussian-player"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def validate_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise ValueError("manifest schema_version must be 2")
    if manifest.get("payload_encoding", "splat") not in ("splat", "gsp2"):
        raise ValueError("unsupported payload encoding")
    dimensions = manifest.get("tiling", {}).get("dimensions", [])
    if len(dimensions) != 3 or any(not isinstance(value, int) or value < 1 for value in dimensions):
        raise ValueError("spatial grid requires three positive dimensions")
    if manifest.get("scene", {}).get("sampling") != "full":
        raise ValueError("camera-trace player requires full-scene sampling")
    tiles = manifest.get("tiles")
    if not isinstance(tiles, list) or len(tiles) != manifest.get("tiling", {}).get("occupied_tiles"):
        raise ValueError("occupied_tiles does not match the manifest tile count")
    files: list[str] = []
    tracks: list[str] = []
    refinement_total = 0
    for tile in tiles:
        refinements = tile.get("refinements")
        if not isinstance(refinements, list) or [item.get("index") for item in refinements] != [0, 1, 2, 3]:
            raise ValueError(f"tile {tile.get('id')} must contain refinement indices 0..3")
        count = sum(int(item.get("gaussian_count", -1)) for item in refinements)
        if count != tile.get("gaussian_count"):
            raise ValueError(f"tile {tile.get('id')} refinement Gaussian accounting is inconsistent")
        refinement_total += count
        for refinement in refinements:
            file_name = refinement.get("file")
            track = refinement.get("track")
            if not isinstance(file_name, str) or not isinstance(track, str):
                raise ValueError("every refinement requires string file and track fields")
            files.append(file_name); tracks.append(track)
            if not (path.parent / file_name).is_file():
                raise ValueError(f"missing refinement file: {file_name}")
    if len(files) != len(set(files)) or len(tracks) != len(set(tracks)):
        raise ValueError("refinement file and track names must be unique")
    if refinement_total != manifest.get("scene", {}).get("gaussian_count"):
        raise ValueError("scene Gaussian accounting is inconsistent")
    return manifest


def scene_archive(manifest_path: Path, destination: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    with tarfile.open(destination, 'w') as archive:
        archive.add(manifest_path, arcname='manifest-v2.json', recursive=False)
        for tile in manifest['tiles']:
            for refinement in tile['refinements']:
                name = refinement['file']
                path = (manifest_path.parent / name).resolve()
                if not path.is_relative_to(manifest_path.parent.resolve()):
                    raise ValueError(f'refinement path escapes scene directory: {name}')
                archive.add(path, arcname=name, recursive=False)


def validate_trace(path: Path) -> list[dict[str, Any]]:
    trace = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(trace, list) or not trace:
        raise ValueError("camera trace must contain at least one frame")
    for index, frame in enumerate(trace):
        timestamp = frame.get("timestamp_ms")
        if not isinstance(timestamp, (int, float)) or (index == 0 and timestamp != 0) or (
                index > 0 and timestamp <= trace[index - 1]["timestamp_ms"]):
            raise ValueError("camera trace must contain strictly ordered timestamps starting at zero")
        required = ("camera_position", "view_matrix", "fov") if "view_matrix" in frame else (
            "camera_position", "camera_forward", "camera_up", "yaw_degrees", "fov", "aspect_ratio")
        if not all(key in frame for key in required):
            raise ValueError(f"camera trace frame {index} is incomplete")
    return trace


def browser_command(url: str, output: Path, telemetry: Path, *, timeout_ms: int,
                    width: int, height: int, headless: bool, record: bool, gpu_mode: str = "hardware") -> list[str]:
    if timeout_ms <= 0 or min(width, height) <= 0:
        raise ValueError("browser timeout and dimensions must be positive")
    command = ["node", str(ROOT / "tools/l4s/camera_trace_browser_runner.mjs"),
               "--url", url, "--telemetry", str(telemetry), "--timeout-ms", str(timeout_ms),
               "--width", str(width), "--height", str(height), "--gpu-mode", gpu_mode]
    if record: command.extend(("--output", str(output)))
    else: command.append("--no-record")
    if headless: command.append("--headless")
    return command


def conversion_commands(webm: Path, output: Path, format_name: str, fps: int, width: int,
                        duration_ms: int) -> list[list[str]]:
    commands: list[list[str]] = []
    duration = f"{duration_ms / 1000:.3f}"
    if format_name in ("mp4", "all"):
        commands.append(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(webm),
                         "-vf", f"tpad=stop_mode=clone:stop_duration={duration},fps={fps}", "-t", duration,
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                         str(output / "camera-trace.mp4")])
    if format_name in ("gif", "all"):
        graph = (f"tpad=stop_mode=clone:stop_duration={duration},fps=15:round=up,"
                 f"scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse")
        commands.append(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(webm),
                         "-filter_complex", graph, "-t", duration, str(output / "camera-trace.gif")])
    return commands


def video_last_pts_seconds(path: Path) -> float:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True)
    timestamps = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    if not timestamps or max(timestamps) <= 0:
        raise RuntimeError(f"recording has no usable video timestamps: {path}")
    return max(timestamps)


def normalize_webm_command(raw: Path, webm: Path, fps: int, duration_ms: int,
                           source_last_pts: float) -> list[str]:
    duration = f"{duration_ms / 1000:.3f}"
    # Hold the final captured pose across the last two CFR slots. MediaRecorder's
    # final sparse frame otherwise has zero duration and the fps filter drops it.
    target_last_pts = duration_ms / 1000 - 2 / fps
    scale = target_last_pts / source_last_pts
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(raw),
            "-vf", f"setpts={scale:.12f}*PTS,tpad=stop_mode=clone:stop_duration=1,fps={fps}",
            "-t", duration,
            "-an", "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", str(webm)]


def wait_port(port: int, processes: list[subprocess.Popen[bytes]], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() not in (None, 0) for process in processes):
            raise RuntimeError("a playback component exited during startup")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"port {port} did not become ready")


def wait_file(path: Path, processes: list[subprocess.Popen[bytes]], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file(): return
        if any(process.poll() not in (None, 0) for process in processes):
            raise RuntimeError("a playback component exited before publisher readiness")
        time.sleep(0.05)
    raise TimeoutError(f"publisher readiness file was not created: {path}")


def terminate(processes: list[subprocess.Popen[bytes]]) -> dict[str, int | None]:
    for process in reversed(processes):
        if process.poll() is None: process.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 5
    for process in reversed(processes):
        try: process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
    return {str(process.args): process.returncode for process in processes}


def run(args) -> int:
    interactive = args.interactive
    validation = interactive or args.fixed_frame or args.camera_check or args.transport == 'http' or args.network_path == 'qemu'
    record = not args.no_record
    overlays = (interactive or not validation) and not args.quality
    dye = not args.no_dye and (interactive or not validation) and not args.quality
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
    required = [PLAYER / 'dist/index.html', PLAYER / 'dist/assets']
    if args.transport == 'moqt' and args.network_path == 'loopback':
        required += [ROOT / 'build/priority-moq-publisher', ROOT / 'build/priority-moq-sidecar',
                     ROOT / 'deps/imquic/examples/imquic-moq-relay']
    for path in required:
        if not path.exists():
            raise ValueError(f'missing build artifact: {path}; build the player or native MoQ fixtures first')
    if (record or args.quality) and shutil.which('ffmpeg') is None:
        raise ValueError('ffmpeg is required to normalize recordings')
    output.mkdir(parents=True)
    raw_webm, webm = output / 'camera-trace.raw.webm', output / 'camera-trace.webm'
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
    comparison = None
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
                topology_path = args.topology_script.expanduser().resolve()
                shutil.copy2(topology_path, output / 'player-topology.py')
                command = [sys.executable, str(ROOT / 'tools/l4s/run_qemu_timeseries_test.py'),
                    '--base', str(args.qemu_base.expanduser().resolve()), '--make-target', 'l4s-camera-player-guest-check',
                    '--guest-result-name', 'player', '--guest-result-root', 'results/l4s', '--destination-root', 'results/l4s',
                    '--destination-prefix', prefix, '--allow-dirty', '--memory', str(getattr(args, 'qemu_memory', 4096)),
                    '--cpus', str(getattr(args, 'qemu_cpus', 4)),
                    '--guest-timeout', str(int(args.timeout_seconds + 900)), '--forward-sidecar-port', str(ports['bridge']),
                    '--guest-file', f'{archive}=inputs/player-scene.tar', '--guest-file', f'{node}=inputs/node',
                    '--guest-file', f'{output / "player-topology.py"}=inputs/player-topology.py',
                    '--make-variable', 'CAMERA_PLAYER_TOPOLOGY_SCRIPT=inputs/player-topology.py',
                    '--make-variable', 'CAMERA_PRIORITY_SCENE_ARCHIVE=inputs/player-scene.tar', '--make-variable',
                    'CAMERA_PRIORITY_NODE=inputs/node', '--make-variable', f'CAMERA_PLAYER_MODE={args.transport_mode}',
                    '--make-variable', f'CAMERA_PLAYER_DURATION={args.timeout_seconds}', '--make-variable',
                    f"CAMERA_PLAYER_ORIGIN=http://127.0.0.1:{ports['web']}", '--make-variable',
                    f'CAMERA_PLAYER_ADMISSION_DELAY_US={args.admission_delay_us}', '--make-variable',
                    f'CAMERA_PLAYER_BOTTLENECK={args.bottleneck or ""}', '--make-variable',
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
                'autoplay': '0' if args.fixed_frame else '1', 'overlays': str(int(overlays)), 'dye': str(int(dye)), 'record': str(int(record)),
                'recordWidth': str(args.width), 'recordHeight': str(args.height), 'recordFps': str(args.fps), 'run': output.name,
                'sidecar': f"ws://127.0.0.1:{ports['bridge']}", 'nonce': str(time.time_ns())}
            if interactive: params.update({'play': '1', 'autoplay': '0'})
            url = f"http://127.0.0.1:{ports['web']}/?{urlencode(params)}"
            write_experiment_record(output, scenario='camera-player-validation' if validation else 'camera-trace-browser-visualization',
                configuration={'manifest': str(manifest_path), 'manifest_sha256': sha256(manifest_path),
                    'source_sha256': manifest['scene']['source_sha256'], 'gaussian_count': manifest['scene']['gaussian_count'],
                    'trace': str(trace_path), 'trace_sha256': sha256(trace_path), 'fixed_pose': trace[0],
                    'width': args.width, 'height': args.height, 'transport': args.transport, 'network_path': args.network_path,
                    'controller': args.transport_mode, 'payload_encoding': manifest.get('payload_encoding', 'splat'),
                    'background': 'black', 'overlays': overlays, 'dye': dye, 'record': record,
                    'gpu_mode': args.gpu_mode, 'recording': {'width': args.width, 'height': args.height, 'fps': args.fps, 'format': args.format},
                    'play_required': interactive, 'interactive': interactive, 'threshold_ssim': .90, 'ports': ports,
                    'admission_delay_us': args.admission_delay_us, 'bottleneck_override': args.bottleneck,
                    'quality_requested': args.quality, 'quality_fps': args.quality_fps,
                    'topology_script': str(args.topology_script),
                    'topology_sha256': sha256(output / 'player-topology.py') if args.network_path == 'qemu' else None},
                topology='host browser -> forwarded WebSocket -> guest client IMQUIC -> DualPI2 -> relay/publisher'
                    if args.network_path == 'qemu' else 'browser -> HTTP or loopback WebSocket/IMQUIC -> relay/publisher',
                observed_network={'launcher_processes': [process.pid for process in processes]})
            if interactive:
                (output / 'endpoint.json').write_text(json.dumps({'url': url, 'ports': ports,
                    'bottleneck': args.bottleneck, 'controller': args.transport_mode,
                    'play_required': True, 'dye': dye, 'pid': os.getpid()}, indent=2) + '\n')
                print(f'Interactive player ready: {url}', flush=True)
                while all(process.poll() is None for process in processes): time.sleep(.5)
                raise RuntimeError('Interactive service exited; see service logs')
            command = browser_command(url, raw_webm, output / 'player-telemetry.json',
                timeout_ms=int(args.timeout_seconds * 1000), width=args.width, height=args.height,
                headless=args.headless, record=record, gpu_mode=args.gpu_mode)
            if args.fixed_frame: command += ['--snapshot', str(output / 'browser.png')]
            if args.quality: command += ['--capture-frames', str(output / 'candidate-frames'), '--capture-fps', str(args.quality_fps)]
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
            if args.quality and browser_result == 0:
                reference_port = free_port(socket.SOCK_STREAM)
                reference_web = start('reference-web', [sys.executable,
                    str(ROOT / 'tools/l4s/camera_trace_player_server.py'), '--dist', str(output / 'player-ui'),
                    '--manifest', str(manifest_path), '--trace', str(trace_path), '--port', str(reference_port)])
                wait_port(reference_port, [reference_web])
                reference_url = f'http://127.0.0.1:{reference_port}/?' + urlencode({
                    'transport': 'http', 'trace': '/camera-trace.json', 'fixedFrame': '1',
                    'autoplay': '0', 'overlays': '0', 'dye': '0', 'record': '0'})
                reference_command = browser_command(reference_url, output / 'unused.webm',
                    output / 'reference-telemetry.json', timeout_ms=int(args.timeout_seconds * 1000),
                    width=args.width, height=args.height, headless=args.headless, record=False, gpu_mode=args.gpu_mode)
                reference_command += ['--capture-frames', str(output / 'reference-frames'),
                    '--reference-frames', str(output / 'candidate-frames/frames.json'), '--motion-trace', str(trace_path)]
                reference_browser = start('reference-browser', reference_command)
                if reference_browser.wait(timeout=args.timeout_seconds + 45):
                    raise RuntimeError('same-player reference rendering failed; see reference-browser.log')
                from compare_player_sequence import compare_sequence
                comparison = compare_sequence(output / 'candidate-frames/frames.json',
                    output / 'reference-frames/frames.json', output / 'quality')

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
    if browser_result == 0 and failure is None and record:
        try:
            duration_ms = int(trace[-1]['timestamp_ms'])
            subprocess.run(normalize_webm_command(raw_webm, webm, args.fps, duration_ms,
                video_last_pts_seconds(raw_webm)), cwd=ROOT, check=True)
            for command in conversion_commands(webm, output, args.format, args.fps, args.width, duration_ms):
                subprocess.run(command, cwd=ROOT, check=True)
            raw_webm.unlink()
        except Exception as error:
            failure = f'{type(error).__name__}: {error}'
            (output / 'failure.txt').write_text(failure + '\n')
    telemetry_path = output / 'player-telemetry.json'
    telemetry = json.loads(telemetry_path.read_text()) if telemetry_path.exists() else {}
    complete = telemetry.get('receivedGaussians') == manifest['scene']['gaussian_count'] and telemetry.get('appliedGaussians') == manifest['scene']['gaussian_count']
    success = (browser_result == 0 and failure is None and complete and telemetry.get('rejected') == 0
        and telemetry.get('duplicates') == 0 and not telemetry.get('failure') and not telemetry.get('runnerFailure')
        and (telemetry.get('renderReady') if args.fixed_frame else telemetry.get('trace', {}).get('state') == 'complete'))
    if record:
        success = success and webm.is_file() and webm.stat().st_size > 0
    summary = {'status': 'PASS' if success else 'FAIL', 'failure': failure, 'browser_exit_code': browser_result,
        'complete_scene': complete, 'transport': args.transport, 'network_path': args.network_path,
        'controller': args.transport_mode, 'process_exit_codes': exits,
        'rendering': comparison if args.quality else 'NOT_EVALUATED'}
    if not validation:
        artifacts = {path.name: {'bytes': path.stat().st_size, 'sha256': sha256(path)}
            for path in (webm, output / 'camera-trace.mp4', output / 'camera-trace.gif') if path.is_file()}
        visualization = {'status': summary['status'], 'visualization_only': True,
            'browser_exit_code': browser_result, 'process_exit_codes': exits, 'artifacts': artifacts,
            'delivery': {key: telemetry.get(key) for key in ('sourceGaussians', 'receivedGaussians',
                'appliedGaussians', 'renderedMeshes', 'duplicates', 'rejected')},
            'trace': telemetry.get('trace'), 'recording': telemetry.get('recording')}
        (output / 'visualization-summary.json').write_text(json.dumps(visualization, indent=2) + '\n')
    (output / 'validation-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({key: value for key, value in summary.items() if key != 'process_exit_codes'}))
    return 0 if success else 1



def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("webm", "mp4", "gif", "all"), default="webm")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--transport-mode", choices=("reno", "prague"), default="reno")
    parser.add_argument("--transport", choices=("http", "moqt"), default="moqt")
    parser.add_argument("--network-path", choices=("loopback", "qemu"), default="loopback")
    parser.add_argument("--fixed-frame", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--camera-check", action="store_true", help="validate rendered recorded/yaw poses after full loading")
    parser.add_argument("--qemu-base", type=Path, default=Path("~/sandbox/p4/work.qcow2"))
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--admission-delay-us", type=int, default=0)
    parser.add_argument("--bottleneck", help="Optional tc rate override; otherwise use the topology script")
    parser.add_argument("--topology-script", type=Path, default=ROOT / "tools/l4s/player_topology.py")
    parser.add_argument("--quality", action="store_true", help="Sample clean frames and compare with a full-scene same-player reference")
    parser.add_argument("--quality-fps", type=float, default=2, help="Similarity capture rate; independent of video FPS")
    parser.add_argument("--no-dye", action="store_true")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--headless", action="store_true", help="CI/testing override; visible Chromium is the default")
    parser.add_argument("--qemu-memory", type=int, default=4096, help="Guest RAM in MiB")
    parser.add_argument("--qemu-cpus", type=int, default=4, help="Guest vCPUs and parallel build jobs")
    parser.add_argument("--gpu-mode", choices=("hardware", "software"), default="hardware")
    args = parser.parse_args(argv)
    if min(args.timeout_seconds, args.qemu_memory, args.qemu_cpus, args.width, args.height, args.fps) <= 0:
        parser.error("timeout, QEMU budgets, dimensions, and fps must be positive")
    if args.admission_delay_us < 0: parser.error("admission delay must be nonnegative")
    if args.interactive and args.fixed_frame: parser.error("interactive playback cannot use --fixed-frame")
    if args.camera_check and not args.fixed_frame: parser.error("--camera-check requires --fixed-frame")
    if args.network_path == "qemu" and args.transport != "moqt": parser.error("QEMU path requires MoQ transport")
    if args.interactive or args.fixed_frame or args.camera_check:
        if not args.no_record:
            parser.error("fixed-frame/interactive validation requires --no-record")
    if args.quality and (args.fixed_frame or args.interactive or args.camera_check):
        parser.error("--quality requires automated trace playback")
    if not 0 < args.quality_fps <= 60:
        parser.error("quality FPS must be positive and at most 60")
    if min(args.width, args.height, args.fps) <= 0: parser.error("width, height, and fps must be positive")
    if args.no_record and args.format != "webm": parser.error("video conversion requires recording")
    return args


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except (OSError, ValueError) as error:
        print(f"player launcher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
