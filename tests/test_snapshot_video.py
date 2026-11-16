from pathlib import Path
import json
import tempfile
import unittest

from tools.l4s.snapshot_video import snapshot_video_command


class SnapshotVideoTest(unittest.TestCase):
    def test_exact_trace_grid_becomes_exact_video_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frames = [{'index': i, 'file': f'frame-{i:06d}.png', 'timestampMs': i * 100} for i in range(3)]
            for frame in frames:
                (root / frame['file']).write_bytes(b'fixture')
            manifest = root / 'frames.json'
            manifest.write_text(json.dumps({'complete': True, 'expectedFrames': 3, 'frames': frames}))
            command = snapshot_video_command(manifest, root / 'reference.mp4')
            self.assertEqual(command[command.index('-framerate') + 1], '10')
            self.assertEqual(command[command.index('-frames:v') + 1], '3')
            self.assertNotIn('-vf', command)
            self.assertNotIn('setpts', ' '.join(command))
            for invalid in ([frames[1], frames[2]], [frames[0], frames[2]]):
                manifest.write_text(json.dumps({'complete': True, 'frames': invalid}))
                with self.assertRaisesRegex(ValueError, 'grid|index|frame'):
                    snapshot_video_command(manifest, root / 'reference.mp4')
            frames[0]['nominalTimestampMs'] = float('nan')
            manifest.write_text(json.dumps({'complete': True, 'frames': frames}))
            with self.assertRaisesRegex(ValueError, 'nominal'):
                snapshot_video_command(manifest, root / 'reference.mp4')
            frames[0].pop('nominalTimestampMs')
            manifest.write_text(json.dumps({'complete': False, 'frames': frames}))
            with self.assertRaisesRegex(ValueError, 'complete'):
                snapshot_video_command(manifest, root / 'reference.mp4')


if __name__ == '__main__':
    unittest.main()
