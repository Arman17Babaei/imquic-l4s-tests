"""Encode complete, trace-aligned PNG snapshots without wall-clock retiming."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess


def sample_snapshot_manifest(manifest: Path, fps: float, source_fps: int) -> Path:
    """Select a quality grid from complete video frames without copying pixels."""
    record = json.loads(manifest.read_text())
    if not isinstance(source_fps, int) or source_fps <= 0 or not math.isfinite(fps) or fps <= 0:
        raise ValueError('quality sampling requires positive finite frame rates')
    stride = source_fps / fps
    if stride < 1 or not math.isclose(stride, round(stride), rel_tol=0, abs_tol=1e-9):
        raise ValueError('ideal quality FPS must divide video FPS')
    frames = record.get('frames', [])
    if record.get('complete') is not True or len(frames) != record.get('expectedFrames', len(frames)):
        raise ValueError('ideal quality sampling requires a complete capture')
    selected = []
    for index, frame in enumerate(frames[::round(stride)]):
        if abs(frame['timestampMs'] - index * 1000 / fps) > 1e-6:
            raise ValueError('quality sample does not lie on the exact frame grid')
        selected.append(dict(frame, index=index, slot=index, nominalTimestampMs=index * 1000 / fps))
    result = dict(record, requestedFps=fps, sourceCaptureFps=source_fps,
                  expectedFrames=len(selected), capturedFrames=len(selected), missingSlots=[], frames=selected)
    target = manifest.with_name('quality-frames.json')
    target.write_text(json.dumps(result, indent=2) + '\n')
    return target


def snapshot_video_command(manifest: Path, output: Path, fps: int = 10) -> list[str]:
    if not isinstance(fps, int) or fps <= 0:
        raise ValueError('snapshot FPS must be a positive integer')
    record = json.loads(manifest.read_text())
    frames = record.get('frames', [])
    if record.get('complete') is not True or not frames:
        raise ValueError('snapshot sequence must be complete and nonempty')
    if record.get('expectedFrames', len(frames)) != len(frames):
        raise ValueError('snapshot sequence has missing frames')
    for index, frame in enumerate(frames):
        expected_file = f'frame-{index:06d}.png'
        timestamp = frame.get('timestampMs')
        if (frame.get('index') != index or frame.get('file') != expected_file or
                not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or
                abs(timestamp - index * 1000 / fps) > 1e-6):
            raise ValueError('snapshot indices/files/timestamps must form the exact zero-origin grid')
        nominal = frame.get('nominalTimestampMs', timestamp)
        if not isinstance(nominal, (int, float)) or not math.isfinite(nominal) or abs(nominal - timestamp) > 1e-6:
            raise ValueError('snapshot nominal and trace timestamps differ')
        if not (manifest.parent / expected_file).is_file():
            raise ValueError(f'missing snapshot frame: {expected_file}')
    suffix = output.suffix.lower()
    if suffix not in ('.mp4', '.webm'):
        raise ValueError('snapshot video must be MP4 or WebM')
    codec = ['-c:v', 'libx264', '-crf', '18', '-movflags', '+faststart'] if suffix == '.mp4' else [
        '-c:v', 'libvpx-vp9', '-crf', '18', '-b:v', '0']
    return ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-n',
            '-framerate', str(fps), '-start_number', '0', '-i', str(manifest.parent / 'frame-%06d.png'),
            '-frames:v', str(len(frames)), *codec, '-pix_fmt', 'yuv420p', '-threads', '2', str(output)]


def encode_snapshot_video(manifest: Path, output: Path, fps: int = 10) -> dict:
    command = snapshot_video_command(manifest, output, fps)
    if output.exists():
        raise ValueError(f'refusing to overwrite video: {output}')
    subprocess.run(command, check=True)
    probe = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
        '-show_entries', 'stream=nb_read_frames,r_frame_rate,start_time', '-of', 'json', str(output)],
        capture_output=True, text=True, check=True).stdout)['streams'][0]
    frames = json.loads(manifest.read_text())['frames']
    numerator, denominator = map(int, probe['r_frame_rate'].split('/'))
    if int(probe['nb_read_frames']) != len(frames) or numerator / denominator != fps or float(probe['start_time']) != 0:
        raise RuntimeError('encoded video count, cadence or zero origin differs from snapshots')
    return {'source': 'trace-grid PNG snapshots', 'fps': fps, 'frames': len(frames), 'start_time_seconds': 0,
            'duration_seconds': len(frames) / fps, 'wall_clock_retiming': False,
            'snapshot_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'video_sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'probe': probe}
