from pathlib import Path
import json
import tempfile
import unittest

from tools.l4s.snapshot_video import snapshot_video_command
import tools.l4s.snapshot_video as snapshots


class SnapshotVideoTest(unittest.TestCase):
    def test_quality_sampling_preserves_images_and_exact_frame_poses(self):
        self.assertTrue(hasattr(snapshots, 'sample_snapshot_manifest'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frames = [dict(index=i, slot=i, file=f'frame-{i:06d}.png', timestampMs=i*1000/30,
                           nominalTimestampMs=i*1000/30, cameraPose={'id':i}) for i in range(30)]
            manifest = root/'frames.json'
            manifest.write_text(json.dumps(dict(complete=True, requestedFps=30, durationMs=1000,
                                               expectedFrames=30, frames=frames)))
            sampled = snapshots.sample_snapshot_manifest(manifest, 5, 30)
            data = json.loads(sampled.read_text())
            self.assertEqual([f['cameraPose']['id'] for f in data['frames']], [0,6,12,18,24])
            self.assertEqual([f['slot'] for f in data['frames']], [0,1,2,3,4])
            self.assertEqual(data['expectedFrames'],5)
            self.assertEqual(data['frames'][1]['file'],'frame-000006.png')
            with self.assertRaises(ValueError):
                snapshots.sample_snapshot_manifest(manifest,7,30)

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
