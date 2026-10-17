"""CUDA regression: exact projection culling preserves full-scene rendering."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/l4s'))


class ReferenceCullingTest(unittest.TestCase):
    def test_culling_matches_unculled_sh3_render(self):
        import numpy as np
        import torch
        from PIL import Image
        from plyfile import PlyData, PlyElement
        from compare_player_frame import render_reference
        from render_3dgs_reordering import activate_render_dependency
        if not torch.cuda.is_available():
            self.skipTest('CUDA required for the exact SH3 renderer')
        activate_render_dependency(ROOT / 'deps/3dgs_over_moq')
        from streaming.transport.client.rasterizer import _render_gsplat
        rng = np.random.default_rng(123)
        count = 200
        names = (['x', 'y', 'z', 'opacity'] + [f'f_dc_{i}' for i in range(3)]
                 + [f'f_rest_{i}' for i in range(45)] + [f'scale_{i}' for i in range(3)]
                 + [f'rot_{i}' for i in range(4)])
        vertices = np.zeros(count, dtype=[(name, '<f4') for name in names])
        means = rng.normal(size=(count, 3)).astype('f4')
        means[:, 2] = np.where(np.arange(count) < 100, -3., 3.)
        for i, axis in enumerate(('x', 'y', 'z')): vertices[axis] = means[:, i]
        sh = rng.normal(0, .1, size=(count, 16, 3)).astype('f4')
        for i in range(3): vertices[f'f_dc_{i}'] = sh[:, 0, i]
        channel_major = sh[:, 1:, :].transpose(0, 2, 1).reshape(count, 45)
        for i in range(45): vertices[f'f_rest_{i}'] = channel_major[:, i]
        for i in range(3): vertices[f'scale_{i}'] = -2.5
        vertices['rot_0'] = 1
        frame = {'view_matrix': np.eye(4).tolist(), 'fov': 60.}
        focal = 64 / np.tan(np.radians(30))
        intrinsic = torch.tensor([[focal, 0, 64], [0, focal, 64], [0, 0, 1]], dtype=torch.float32)
        expected = _render_gsplat(torch.from_numpy(means), torch.zeros(count, 1),
            torch.from_numpy(sh.reshape(count, 48)), torch.full((count, 3), -2.5),
            torch.tensor([[1., 0, 0, 0]]).repeat(count, 1), torch.eye(4),
            intrinsic, 128, 128, torch.zeros(3), 'cuda:0').numpy()
        with tempfile.TemporaryDirectory(dir=ROOT / 'results') as directory:
            source, destination = Path(directory) / 'source.ply', Path(directory) / 'reference.png'
            PlyData([PlyElement.describe(vertices, 'vertex')]).write(source)
            runtime = render_reference(source, frame, destination, 128, 128)
            self.assertLess(runtime['projected_gaussians'], count)
            np.testing.assert_array_equal(np.asarray(Image.open(destination)), expected)


if __name__ == '__main__':
    unittest.main()
