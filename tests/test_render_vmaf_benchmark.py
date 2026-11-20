import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from benchmark_render_vmaf import bounded_trace


class BoundedTraceTests(unittest.TestCase):
    def test_holds_final_pose_without_stretching(self):
        trace = [{'timestamp_ms': 0, 'camera_position': [0, 0, 0]},
                 {'timestamp_ms': 30, 'camera_position': [3, 6, 9]}]
        result = bounded_trace(trace, 20)
        self.assertEqual(result[-1], {'timestamp_ms': 20, 'camera_position': [0, 0, 0]})
        self.assertEqual(trace[-1]['timestamp_ms'], 30)

    def test_exact_boundary(self):
        trace = [{'timestamp_ms': x} for x in (0, 10, 20, 30)]
        self.assertEqual(bounded_trace(trace, 20), trace[:3])

    def test_rejects_short_or_unordered_trace(self):
        for times in ((0, 10), (1, 30), (0, 30, 20)):
            with self.assertRaises(ValueError):
                bounded_trace([{'timestamp_ms': x} for x in times], 20)
