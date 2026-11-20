import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from benchmark_render_vmaf import bounded_trace, validate_candidate, render_parallel
from unittest.mock import patch
from types import SimpleNamespace
import json
import tempfile
from prepare_benchmark_heartbeat import BEFORE, AFTER, prepare, ROOT


class BoundedTraceTests(unittest.TestCase):
    def test_parallel_merge_preserves_grid_and_disjoint_cpus(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'results') as temporary:
            root = Path(temporary).resolve()
            args = SimpleNamespace(render_workers=2, manifest=root / 'scene.json', dist=root,
                fps=1, width=320, height=180, gpu_mode='llvmpipe', timeout_seconds=10)
            def run(command, **kwargs):
                shard = Path(command[command.index('--output') + 1])
                start, end = (int(command[command.index(flag) + 1]) for flag in ('--frame-start', '--frame-end'))
                directory = shard / 'reference-frames'
                directory.mkdir(parents=True)
                frames = []
                for local, slot in enumerate(range(start, end)):
                    file = f'frame-{local:06d}.png'
                    (directory / file).write_bytes(f'pixel-{slot}'.encode())
                    frames.append(dict(index=local, timestampMs=slot * 1000, file=file, appliedGaussians=42,
                        cameraPose=dict(viewMatrix=[1], position=[0, 0, 0], up=[0, 1, 0], fov=50, aspect=1)))
                (directory / 'frames.json').write_text(json.dumps(dict(complete=True, frames=frames)))
                (shard / 'reference-summary.json').write_text(json.dumps(dict(complete=True, appliedGaussians=42)))
            with patch('benchmark_render_vmaf.os.sched_getaffinity', return_value={0, 1, 2, 3}), \
                    patch('benchmark_render_vmaf.subprocess.run', side_effect=run), \
                    patch('snapshot_video.encode_snapshot_video', return_value={'frames': 4}):
                render_parallel(args, root, 4)
            summary = json.loads((root / 'render/reference-summary.json').read_text())
            self.assertEqual([shard['cpus'] for shard in summary['shards']], [[0, 1], [2, 3]])
            frames = json.loads((root / 'render/reference-frames/frames.json').read_text())['frames']
            self.assertEqual([frame['timestampMs'] for frame in frames], [0, 1000, 2000, 3000])
            self.assertEqual((root / 'render/reference-frames/frame-000002.png').read_bytes(), b'pixel-2')

    def test_heartbeat_adapter_is_guarded_and_preserves_original(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'results') as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / 'assets').mkdir(parents=True)
            asset = source / 'assets/main.js'
            asset.write_text(BEFORE)
            prepare(source, root / 'patched')
            self.assertEqual(asset.read_text(), BEFORE)
            self.assertEqual((root / 'patched/assets/main.js').read_text(), AFTER)
            prepare(source, root / 'manual', manual_updates=True)
            self.assertEqual((root / 'manual/assets/main.js').read_text(), 'F.autoUpdate=!1,' + BEFORE)
            with self.assertRaises(ValueError):
                prepare(source, root / 'patched')
            with self.assertRaisesRegex(ValueError, 'readback implementation'):
                prepare(source, root / 'unsupported-readback', synchronous_readback=True)
            self.assertFalse((root / 'unsupported-readback').exists())
            with self.assertRaisesRegex(ValueError, 'profiling requires'):
                prepare(source, root / 'unsupported-profile', profile=True)
            self.assertFalse((root / 'unsupported-profile').exists())
            with self.assertRaisesRegex(ValueError, 'read pause implementation'):
                prepare(source, root / 'unsupported-pause', no_read_pause=True)
            self.assertFalse((root / 'unsupported-pause').exists())
            prepare(source, root / 'profile', manual_updates=True, profile=True)
            self.assertIn('PROFILE seek_update_ms', (root / 'profile/assets/main.js').read_text())
            asset.write_text('unknown build')
            with self.assertRaisesRegex(ValueError, 'unsupported player'):
                prepare(source, root / 'unsupported')

    def test_candidate_must_match_grid(self):
        stream = dict(width=640, height=360, nb_read_frames='200', r_frame_rate='10/1', start_time='0')
        def validate(overrides):
            result = SimpleNamespace(stdout=json.dumps({'streams': [dict(stream, **overrides)]}))
            with patch('benchmark_render_vmaf.subprocess.run', return_value=result):
                validate_candidate(Path('/ffmpeg'), Path('/video'), fps=10, width=640, height=360, frames=200)
        validate({})
        for overrides in ({'width': 320}, {'nb_read_frames': '199'}, {'r_frame_rate': '30/1'},
                          {'start_time': '1'}, {'avg_frame_rate': '9/1'}, {'duration': '21'}):
            with self.assertRaises(ValueError):
                validate(overrides)

    def test_holds_final_pose_without_stretching(self):
        trace = [{'timestamp_ms': 0, 'camera_position': [0, 0, 0]},
                 {'timestamp_ms': 30, 'camera_position': [3, 6, 9]}]
        result = bounded_trace(trace, 20)
        self.assertEqual(result[-1], {'timestamp_ms': 20, 'camera_position': [0, 0, 0]})
        self.assertEqual(trace[-1]['timestamp_ms'], 30)

    def test_sparse_boundary_cannot_change_sampled_poses(self):
        with self.assertRaisesRegex(ValueError, 'sparse trace boundary'):
            bounded_trace([{'timestamp_ms': 0}, {'timestamp_ms': 30000}], 20000)

    def test_exact_boundary(self):
        trace = [{'timestamp_ms': x} for x in (0, 10, 20, 30)]
        self.assertEqual(bounded_trace(trace, 20), trace[:3])

    def test_rejects_short_or_unordered_trace(self):
        for times in ((0, 10), (1, 30), (0, 30, 20), (0, float('nan')), (0, float('inf'))):
            with self.assertRaises(ValueError):
                bounded_trace([{'timestamp_ms': x} for x in times], 20)
