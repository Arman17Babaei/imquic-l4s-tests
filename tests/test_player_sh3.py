import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement

SPEC = importlib.util.spec_from_file_location('prepare_player_sh3', Path(__file__).resolve().parents[1] / 'tools/l4s/prepare_player_sh3.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PlayerSh3Tests(unittest.TestCase):
    def test_adaptive_tiles_bound_fetch_granularity_and_preserve_every_source_id(self):
        names = ['x', 'y', 'z', 'opacity'] + [f'f_dc_{i}' for i in range(3)] + [f'f_rest_{i}' for i in range(45)] + [f'scale_{i}' for i in range(3)] + [f'rot_{i}' for i in range(4)]
        data = np.zeros(20, dtype=[(name, '<f4') for name in names])
        data['x'] = np.arange(20); data['opacity'] = np.arange(20); data['rot_0'] = 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source.ply'
            PlyData([PlyElement.describe(data, 'vertex')], text=False).write(source)
            manifest = MODULE.export_scene(source, root / 'export', grid=(1, 1, 1), chunk=3, max_tile_gaussians=4)
            self.assertEqual(manifest['tiling']['type'], 'adaptive-aabb-grid')
            self.assertTrue(all(tile['gaussian_count'] <= 4 for tile in manifest['tiles']))
            self.assertEqual(sum(tile['gaussian_count'] for tile in manifest['tiles']), 20)
            ids = []
            for tile in manifest['tiles']:
                for layer in tile['refinements']:
                    content = (root / 'export' / layer['file']).read_bytes()
                    for obj in layer['objects']:
                        payload = content[obj['offset']:obj['offset'] + obj['bytes']]
                        self.assertEqual(MODULE.hashlib.sha256(payload).hexdigest(), obj['sha256'])
                        header = struct.unpack_from('<10I', payload)
                        cursor = 40 + header[3] + header[4]
                        size = struct.unpack_from('<I', payload, cursor)[0]
                        ids.extend(np.frombuffer(payload, dtype='<u4', count=size // 4, offset=cursor + 4).tolist())
            self.assertEqual(sorted(ids), list(range(20)))

    def test_export_is_complete_hashed_and_preserves_channel_major_ply_sh(self):
        names = ['x', 'y', 'z', 'opacity'] + [f'f_dc_{i}' for i in range(3)] + [f'f_rest_{i}' for i in range(45)] + [f'scale_{i}' for i in range(3)] + [f'rot_{i}' for i in range(4)]
        data = np.zeros(20, dtype=[(name, '<f4') for name in names])
        data['x'] = np.arange(20); data['opacity'] = np.arange(20)
        data['rot_0'] = 1
        for i in range(45): data[f'f_rest_{i}'] = i + .25
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.ply'
            PlyData([PlyElement.describe(data, 'vertex')], text=False).write(source)
            manifest = MODULE.export_scene(source, root / 'export', grid=(1, 1, 1), chunk=3)
            self.assertEqual(manifest['scene']['gaussian_count'], 20)
            self.assertEqual(manifest['payload_encoding'], 'gsp2')
            seen = []
            for layer in manifest['tiles'][0]['refinements']:
                content = (root / 'export' / layer['file']).read_bytes()
                self.assertEqual(sum(o['gaussian_count'] for o in layer['objects']), layer['gaussian_count'])
                for obj in layer['objects']:
                    payload = content[obj['offset']:obj['offset'] + obj['bytes']]
                    self.assertEqual(MODULE.hashlib.sha256(payload).hexdigest(), obj['sha256'])
                    header = struct.unpack_from('<10I', payload)
                    self.assertEqual(header[5:8], (layer['index'], obj['id'], obj['gaussian_count']))
                    cursor = 40 + header[3] + header[4]
                    arrays = []
                    for _ in range(6):
                        length = struct.unpack_from('<I', payload, cursor)[0]; cursor += 4
                        arrays.append(payload[cursor:cursor+length]); cursor += length
                    seen.extend(np.frombuffer(arrays[0], dtype='<u4').tolist())
                    sh = np.frombuffer(arrays[3], dtype='<f4').reshape(-1, 16, 3)
                    np.testing.assert_equal(sh[0, 1], [.25, 15.25, 30.25])
            self.assertEqual(sorted(seen), list(range(20)))
            self.assertEqual(len(set(seen)), 20)
            with self.assertRaisesRegex(ValueError, 'must not exist'):
                MODULE.export_scene(source, root / 'export')


if __name__ == '__main__':
    unittest.main()
