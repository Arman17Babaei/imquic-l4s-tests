"""Incomplete render captures must never become quality PASS."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/l4s'))
from compare_player_sequence import compare_sequence

class PartialQualityTest(unittest.TestCase):
    def test_partial_scores_keep_incomplete_status_and_default_rejects(self):
        def chunk(kind, data):
            return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+bytes([32,64,96])*16)*16))+chunk(b'IEND',b'')
        with tempfile.TemporaryDirectory(dir='results') as temporary:
            root=Path(temporary);(root/'image.png').write_bytes(png)
            frame=dict(index=0,file='image.png',timestampMs=0,appliedGaussians=0,cameraPose=dict(viewMatrix=[1],position=[0,0,0],up=[0,1,0],fov=50,aspect=1))
            candidate=root/'candidate.json';reference=root/'reference.json'
            candidate.write_text(json.dumps(dict(complete=False,expectedFrames=2,missingSlots=[1],frames=[frame])))
            reference.write_text(json.dumps(dict(complete=True,frames=[frame])))
            with self.assertRaisesRegex(ValueError,'incomplete'):
                compare_sequence(candidate,reference,root/'strict')
            result=compare_sequence(candidate,reference,root/'partial',allow_incomplete=True)
            self.assertEqual(result['status'],'INCOMPLETE')
            self.assertEqual(result['frames'],1)
            self.assertAlmostEqual(result['mean_ssim'],1)
            failed = json.loads(candidate.read_text())
            failed.update(playbackComplete=False, errors=['browser capture failed'])
            candidate.write_text(json.dumps(failed))
            result = compare_sequence(candidate, reference, root / 'failed-runtime',
                                      allow_incomplete=True, max_missing_fraction=.9)
            self.assertEqual(result['status'], 'INCOMPLETE')
            self.assertFalse(result['accepted'])
            self.assertEqual(result['frames'], 1)
            self.assertAlmostEqual(result['mean_ssim'], 1)
            self.assertIn('playback did not complete', result['snapshot_coverage']['acceptance_errors'])
            self.assertIn('browser capture failed', result['snapshot_coverage']['acceptance_errors'])
            self.assertEqual(result['snapshot_coverage']['structural_errors'], [])


class ToleratedComparisonTest(unittest.TestCase):
    def test_partial_comparison_retains_raw_loss_and_exact_available_frames(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir='results') as temporary:
            root = Path(temporary)
            (root / 'image.png').write_bytes(b'fixture; mocked metrics')
            pose = dict(viewMatrix=[1], position=[0, 0, 0], up=[0, 1, 0], fov=50, aspect=1)
            frames = [dict(index=i, slot=i, file='image.png', timestampMs=i * 200,
                           appliedGaussians=0, cameraPose=pose) for i in range(94)]
            candidate, reference = root / 'candidate.json', root / 'reference.json'
            candidate.write_text(json.dumps(dict(complete=False, expectedFrames=95,
                                                missingSlots=[94], frames=frames)))
            reference.write_text(json.dumps(dict(complete=True, frames=frames)))
            values = {i: 1.0 for i in range(94)}
            with patch('compare_player_sequence.cuda_metrics', return_value=(values, values, {})):
                result = compare_sequence(candidate, reference, root / 'quality',
                                          backend='cuda', max_missing_fraction=.05)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(result['frames'], 94)
            self.assertFalse(result['snapshot_coverage']['complete'])
            self.assertEqual(result['snapshot_coverage']['missing_frames'], 1)
            self.assertEqual(len((root / 'quality/frame-quality.csv').read_text().splitlines()), 95)


class SnapshotCoverageTest(unittest.TestCase):
    def coverage(self, available, **extra):
        from compare_player_sequence import snapshot_coverage
        manifest = dict(complete=False, expectedFrames=95,
                        missingSlots=list(range(available, 95)),
                        frames=[dict(slot=i, timestampMs=i * 200) for i in range(available)])
        manifest.update(extra)
        return snapshot_coverage(manifest, .05)

    def test_tolerance_uses_exact_fraction_without_rounding(self):
        self.assertTrue(self.coverage(94)['accepted'])
        self.assertTrue(self.coverage(91)['accepted'])
        self.assertFalse(self.coverage(90)['accepted'])
        self.assertFalse(self.coverage(94)['complete'])

    def test_only_queue_overflow_errors_can_be_covered_by_loss_tolerance(self):
        self.assertTrue(self.coverage(94, errors=['queue overflow at slot 94'])['accepted'])
        self.assertFalse(self.coverage(94, errors=['binding export failed'])['accepted'])
        self.assertFalse(self.coverage(94, playbackComplete=False)['accepted'])

    def test_declared_missing_slots_and_slot_order_must_be_consistent(self):
        self.assertFalse(self.coverage(94, missingSlots=[])['accepted'])
        self.assertFalse(self.coverage(94, frames=[dict(slot=1), dict(slot=1)])['accepted'])
