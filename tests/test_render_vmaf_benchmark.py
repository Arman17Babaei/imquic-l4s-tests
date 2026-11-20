import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from benchmark_render_vmaf import bounded_trace, validate_candidate
from unittest.mock import patch
from types import SimpleNamespace
import json
import tempfile
from prepare_benchmark_heartbeat import BEFORE, AFTER, prepare, ROOT


class BoundedTraceTests(unittest.TestCase):
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
            with self.assertRaises(ValueError):
                prepare(source, root / 'patched')
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
        for times in ((0, 10), (1, 30), (0, 30, 20)):
            with self.assertRaises(ValueError):
                bounded_trace([{'timestamp_ms': x} for x in times], 20)
