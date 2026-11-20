"""Batch ownership/resource accounting without running browsers."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from benchmark_scene_batch import process_tree_usage, SCENES
from validate_scene_batch import validate


class BatchTests(unittest.TestCase):
    def test_validation_checks_pixels_and_poses_for_every_scene(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for side in ('current', 'baseline'):
                for scene, _ in SCENES:
                    directory = root / side / scene / 'render/reference-frames'
                    directory.mkdir(parents=True)
                    (directory / 'frame.png').write_bytes(b'pixels')
                    (directory / 'frames.json').write_text(json.dumps({'complete': True, 'frames': [
                        {'file': 'frame.png', 'timestampMs': 0, 'cameraPose': {'position': [0, 0, 0]},
                         'appliedGaussians': 42}]}))
            self.assertEqual(len(validate(root / 'current', root / 'baseline')['scenes']), 5)
            (root / 'current/train/render/reference-frames/frame.png').write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError, 'PNG differs'):
                validate(root / 'current', root / 'baseline')

    def test_five_distinct_scenes_and_traces(self):
        self.assertEqual(len(SCENES), 5)
        self.assertEqual(len({scene for scene, _ in SCENES}), 5)
        self.assertEqual(len({trace for _, trace in SCENES}), 5)

    def test_tree_rss_and_cpu_exclude_unrelated_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for pid, parent in ((10, 1), (11, 10), (12, 11), (20, 1)):
                fields = ['0'] * 22
                fields[0], fields[1], fields[11], fields[12], fields[21] = 'S', str(parent), '100', '50', '10'
                (root / str(pid)).mkdir()
                (root / str(pid) / 'stat').write_text(f'{pid} (name with spaces) ' + ' '.join(fields))
            with patch('benchmark_scene_batch.Path', return_value=root), \
                    patch('benchmark_scene_batch.os.sysconf', side_effect=lambda key: 100 if key == 'SC_CLK_TCK' else 4096):
                self.assertEqual(process_tree_usage(10), (4.5, 3 * 10 * 4096))
