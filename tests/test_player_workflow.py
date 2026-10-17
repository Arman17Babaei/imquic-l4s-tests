"""CPU-only contracts for the editable topology and timestamped quality path."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zlib
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/l4s'))
import player_topology
from run_camera_player_guest import load_topology
from run_qemu_player import archive_worktree
from compare_player_sequence import compare_sequence
from run_priority_audit_loopback import validate_object_coverage


def png(path: Path, color: int):
    def chunk(name, payload):
        return struct.pack('!I', len(payload)) + name + payload + struct.pack('!I', zlib.crc32(name + payload))
    header = struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0)
    pixels = (b'\0' + bytes([color] * 32 * 3)) * 32
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def frames(root: Path, color=0, timestamp=100):
    root.mkdir()
    png(root / 'frame-000000.png', color)
    manifest = root / 'frames.json'
    manifest.write_text(json.dumps({'complete': True, 'frames': [{'index': 0,
        'file': 'frame-000000.png', 'timestampMs': timestamp, 'appliedGaussians': 2,
        'cameraPose': {'position': [0, 0, 0], 'up': [0, 1, 0],
            'viewMatrix': [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], 'fov': 60, 'aspect': 1}}]}))
    return manifest


class TopologyTests(unittest.TestCase):
    def test_editable_delay_and_rate_reach_separate_qdiscs(self):
        with patch.object(player_topology, 'PROPAGATION_DELAY', '7ms'), patch.object(player_topology, 'BOTTLENECK', '37mbit'):
            delay = player_topology.shaping_commands('s2-eth1', propagation=True)
            aqm = player_topology.shaping_commands('server-eth0', propagation=False)
            self.assertIn('7ms', delay[0])
            self.assertFalse(any('dualpi2' in row for row in delay))
            self.assertFalse(any('netem' in row for row in aqm))
            self.assertIn('37mbit', aqm[1])
            self.assertIn('123mbit', player_topology.shaping_commands('dev', propagation=False, rate='123mbit')[1])

    def test_custom_topology_is_loaded_without_importing_mininet(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'my_topology.py'
            script.write_text('def build_topology():\n    return "my editable graph"\n')
            self.assertEqual(load_topology(script).build_topology(), 'my editable graph')

    def test_dirty_archive_skips_deleted_files_and_preserves_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'gone').write_text('deleted')
            subprocess.run(['git', '-C', str(root), 'add', 'gone'], check=True)
            (root / 'gone').unlink()
            (root / 'live').write_text('retained')
            (root / 'link').symlink_to('missing-target')
            archive_worktree(root, root / 'source.tar.gz')
            with tarfile.open(root / 'source.tar.gz') as archive:
                self.assertNotIn('gone', archive.getnames())
                self.assertEqual(archive.extractfile('live').read(), b'retained')
                self.assertTrue(archive.getmember('link').issym())


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for CPU image metrics')
class QualityTests(unittest.TestCase):
    def test_identical_and_changed_images_are_recomputed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = frames(root / 'candidate')
            reference = frames(root / 'reference')
            exact = compare_sequence(candidate, reference, root / 'quality')
            self.assertEqual(exact['frames'], 1)
            self.assertEqual(exact['mean_ssim'], 1.0)
            self.assertEqual(exact['infinite_psnr_frames'], 1)
            png(reference.parent / 'frame-000000.png', 255)
            changed = compare_sequence(candidate, reference, root / 'quality')
            self.assertLess(changed['mean_ssim'], .01)
            self.assertEqual(changed['infinite_psnr_frames'], 0)

    def test_timestamp_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = frames(root / 'candidate')
            reference = frames(root / 'reference', timestamp=101)
            with self.assertRaisesRegex(ValueError, 'timestamps differ'):
                compare_sequence(candidate, reference, root / 'quality')

    def test_incomplete_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = frames(root / 'candidate')
            reference = frames(root / 'reference')
            data = json.loads(reference.read_text()); data['complete'] = False
            reference.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                compare_sequence(candidate, reference, root / 'quality')


class PriorityAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {"tiles": [{"id": "tile", "refinements": [{"index": 0,
            "objects": [{"id": 0, "bytes": 32, "sha256": "abc",
                         "publisher_priority": 0}]}]}]}
        self.delivery = {"tile_id": "tile", "refinement": "0", "object_id": "0",
            "request_id": "7", "sha256": "abc", "actual_sha256": "abc",
            "payload_bytes": "32", "publisher_priority": "0", "status": "delivered",
            "subscriber_priority": "8", "subscriber_epoch": "1", "monotonic_us": "200"}

    def test_duplicate_or_hash_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_object_coverage(self.manifest, [self.delivery, self.delivery])
        wrong = {**self.delivery, "actual_sha256": "bad"}
        with self.assertRaisesRegex(ValueError, "hash"):
            validate_object_coverage(self.manifest, [wrong])

    def test_complete_object_coverage_is_accepted(self):
        validate_object_coverage(self.manifest, [self.delivery])


if __name__ == '__main__':
    unittest.main()
