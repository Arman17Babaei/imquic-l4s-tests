"""CUDA metric parity against the established RGB FFmpeg comparator."""
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from compare_player_sequence import compare_sequence

@unittest.skipUnless(importlib.util.find_spec('torch'), 'requires PyTorch with CUDA')
class CudaQualityTests(unittest.TestCase):
    def test_cuda_matches_ffmpeg_for_identical_and_changed_images(self):
        import torch
        if not torch.cuda.is_available():
            self.skipTest('requires CUDA')
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory(dir='results') as temporary:
            root = Path(temporary)
            rng = np.random.default_rng(42)
            image = rng.integers(0, 256, (60, 80, 3), dtype=np.uint8)
            pose = dict(viewMatrix=[1, 0], position=[0, 0, 0], up=[0, 1, 0], fov=50, aspect=4/3)
            for side in ('candidate', 'reference'):
                directory = root / side
                directory.mkdir()
                frames = []
                for i in range(3):
                    pixels = image.copy()
                    if side == 'candidate' and i:
                        pixels = np.clip(pixels.astype(int) + i * 12, 0, 255).astype('uint8')
                    picture = Image.fromarray(pixels)
                    picture = picture.convert('RGBA')
                    picture.save(directory / f'{i}.png')
                    frames.append(dict(index=i, file=f'{i}.png', timestampMs=i*500,
                                       cameraPose=pose, appliedGaussians=1))
                (directory / 'frames.json').write_text(json.dumps(dict(complete=True, frames=frames)))
            candidate, reference = (root / side / 'frames.json' for side in ('candidate', 'reference'))
            cpu = compare_sequence(candidate, reference, root / 'cpu')
            gpu = compare_sequence(candidate, reference, root / 'cuda', backend='cuda')
            self.assertEqual(gpu['backend']['device_type'], 'cuda')
            self.assertEqual(gpu['infinite_psnr_frames'], 1)
            import csv
            with (root/'cpu/frame-quality.csv').open() as stream:
                a = list(csv.DictReader(stream))
            with (root/'cuda/frame-quality.csv').open() as stream:
                b = list(csv.DictReader(stream))
            for x, y in zip(a, b):
                self.assertAlmostEqual(float(x['rgb_ssim']), float(y['rgb_ssim']), delta=1e-4)
                if math.isfinite(float(x['psnr_db'])):
                    self.assertAlmostEqual(float(x['psnr_db']), float(y['psnr_db']), delta=.01)
            self.assertEqual(cpu['frames'], gpu['frames'])

class QualityFailureTests(unittest.TestCase):
    def test_cli_cuda_failure_replaces_stale_pass(self):
        from unittest.mock import patch
        import compare_player_sequence as comparator
        for failure in (RuntimeError('CUDA unavailable'), ImportError('torch unavailable')):
            with self.subTest(error=type(failure).__name__), tempfile.TemporaryDirectory(dir='results') as temporary:
                output = Path(temporary)
                (output/'comparison.json').write_text('{"status":"PASS"}')
                arguments = ['compare_player_sequence', '--candidate', 'missing.json',
                             '--reference', 'missing.json', '--output', str(output), '--backend', 'cuda']
                with patch.object(sys, 'argv', arguments), patch.object(comparator, 'compare_sequence', side_effect=failure):
                    self.assertEqual(comparator.main(), 1)
                self.assertEqual(json.loads((output/'comparison.json').read_text())['status'], 'FAIL')
