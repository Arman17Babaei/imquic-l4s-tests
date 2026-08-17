from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from three_dgs_bundle import (
    BUNDLE_HEADER,
    BUNDLE_MAGIC,
    BUNDLE_VERSION,
    bundle_summary,
    cluster_metadata,
    read_bundle,
    write_bundle,
)


class BundleContractTests(unittest.TestCase):
    def test_round_trip_preserves_records_and_header_counts(self):
        records = [b"alpha", bytes(range(32)), b"omega"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.bundle"
            summary = write_bundle(path, records)
            self.assertEqual(list(read_bundle(path)), records)
            self.assertEqual(summary["objects"], 3)
            self.assertEqual(summary["payload_bytes"], sum(map(len, records)))
            with path.open("rb") as stream:
                magic, version, count, payload_bytes = BUNDLE_HEADER.unpack(
                    stream.read(BUNDLE_HEADER.size)
                )
            self.assertEqual(magic, BUNDLE_MAGIC)
            self.assertEqual(version, BUNDLE_VERSION)
            self.assertEqual(count, 3)
            self.assertEqual(payload_bytes, sum(map(len, records)))

    def test_summary_hashes_valid_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.bundle"
            write_bundle(path, [b"a", b"bc"])
            summary = bundle_summary(path)
        self.assertEqual(summary["objects"], 2)
        self.assertEqual(summary["payload_bytes"], 3)
        self.assertEqual(len(summary["sha256"]), 64)

    def test_truncated_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.bundle"
            path.write_bytes(BUNDLE_HEADER.pack(BUNDLE_MAGIC, BUNDLE_VERSION, 1, 5) +
                             struct.pack("<I", 5) + b"abc")
            with self.assertRaisesRegex(ValueError, "truncated record payload"):
                list(read_bundle(path))


class WireRoutingContractTests(unittest.TestCase):
    def frame(self, track=b"track-0007", group=b"3", object_id=9, subgroup_id=2):
        payload = track + group
        header = struct.pack(
            "<8I", 0x47535033, 1, len(track), len(group), object_id,
            0, len(payload), subgroup_id,
        )
        return header + payload

    def test_metadata_preserves_embedded_identity(self):
        metadata = cluster_metadata(self.frame())
        self.assertEqual(metadata["track_id"], "track-0007")
        self.assertEqual(metadata["group_id"], "3")
        self.assertEqual(metadata["subgroup_id"], 2)
        self.assertEqual(metadata["object_id"], 9)

    def test_truncated_metadata_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "shorter"):
            cluster_metadata(b"short")


if __name__ == "__main__":
    unittest.main()
