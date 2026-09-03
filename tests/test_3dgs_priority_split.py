from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from split_3dgs_priority import object_importance, split_priority_bundle
from three_dgs_bundle import read_bundle, write_bundle


def _array(values):
    if values is None:
        return struct.pack("<I", 0)
    encoded = struct.pack(f"<{len(values)}f", *values)
    return struct.pack("<I", len(encoded)) + encoded


def frame(opacities, scales, *, object_id=0, subgroup_id=0, omit_sh=False, omit_opacity=False):
    n = len(opacities)
    assert len(scales) == n
    track = b"track-0001"
    group = b"0"
    means = [0.0] * (n * 3)
    sh = None if omit_sh else [0.0] * (n * 3)
    rotations = [0.0] * (n * 4)
    body = (
        track + group
        + _array(means)
        + _array(None if omit_opacity else opacities)
        + _array(sh)
        + _array([x for scale in scales for x in scale])
        + _array(rotations)
    )
    header = struct.pack(
        "<8I", 0x47535033, 1, len(track), len(group), object_id, n, len(body), subgroup_id
    )
    return header + body


class PriorityParsingTests(unittest.TestCase):
    def test_parses_real_length_prefixed_wire_layout_and_optional_arrays(self):
        payload = frame(
            [0.2, 0.8],
            [(1.0, 2.0, 1.5), (3.0, 1.0, 2.0)],
            object_id=7,
            subgroup_id=2,
            omit_sh=True,
        )
        score = object_importance(payload)
        self.assertAlmostEqual(score["opacity_score"], 0.5)
        self.assertAlmostEqual(score["scale_score"], 2.5)
        self.assertEqual(score["num_gaussians"], 2)
        self.assertEqual(score["track_id"], "track-0001")
        self.assertEqual(score["group_id"], "0")
        self.assertEqual(score["object_id"], 7)
        self.assertEqual(score["subgroup_id"], 2)

    def test_truncated_length_prefixed_array_is_rejected(self):
        with self.assertRaises(ValueError):
            object_importance(frame([0.5], [(1.0, 1.0, 1.0)])[:-8])

    def test_optional_opacity_is_valid_for_wire_parser(self):
        score = object_importance(
            frame([0.5], [(1.0, 1.0, 1.0)], subgroup_id=1, omit_opacity=True)
        )
        self.assertIsNone(score["opacity_score"])
        self.assertAlmostEqual(score["scale_score"], 1.0)

    def test_native_tier_can_use_object_without_opacity_but_opacity_ablation_cannot(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scene.bundle"
            records = [
                frame([0.5], [(1.0, 1.0, 1.0)], object_id=0, subgroup_id=0, omit_opacity=True),
                frame([0.4], [(1.0, 1.0, 1.0)], object_id=1, subgroup_id=2),
            ]
            write_bundle(source, records)
            split_priority_bundle(source, Path(directory) / "native", importance="native-tier")
            with self.assertRaisesRegex(ValueError, "opacity ranking requires"):
                split_priority_bundle(source, Path(directory) / "opacity", importance="opacity")


class PrioritySplitTests(unittest.TestCase):
    def make_source(self, directory: str, rows):
        records = [
            frame([opacity], [(scale,) * 3], object_id=index, subgroup_id=subgroup)
            for index, (opacity, scale, subgroup) in enumerate(rows)
        ]
        source = Path(directory) / "scene.bundle"
        write_bundle(source, records)
        return source, records

    def test_native_tier_dominates_opacity_and_preserves_source_order(self):
        with tempfile.TemporaryDirectory() as directory:
            source, records = self.make_source(directory, [
                (0.01, 1.0, 0),  # low opacity but native highest tier
                (100.0, 1.0, 2),
                (0.02, 1.0, 0),
                (50.0, 1.0, 1),
            ])
            output = Path(directory) / "split"
            manifest = split_priority_bundle(source, output, importance="native-tier")
            self.assertEqual(list(read_bundle(output / "high-priority.bundle")), [records[0], records[2]])
            self.assertEqual(list(read_bundle(output / "low-priority.bundle")), [records[1], records[3]])
            self.assertEqual(manifest["high"]["subgroup_objects"], {"0": 2, "1": 0, "2": 0})
            self.assertEqual(manifest["balance"]["object_fraction_high"], 0.5)

    def test_opacity_ablation_sends_top_half_to_prague(self):
        with tempfile.TemporaryDirectory() as directory:
            source, records = self.make_source(directory, [
                (0.1, 1.0, 0), (0.9, 2.0, 1), (0.2, 3.0, 2), (0.8, 4.0, 2),
            ])
            output = Path(directory) / "split"
            manifest = split_priority_bundle(source, output, importance="opacity")
            self.assertEqual(list(read_bundle(output / "high-priority.bundle")), [records[1], records[3]])
            self.assertEqual(list(read_bundle(output / "low-priority.bundle")), [records[0], records[2]])
            self.assertEqual(manifest["high"]["objects"], 2)
            self.assertEqual(manifest["low"]["objects"], 2)

    def test_scale_mode_uses_scale_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            source, records = self.make_source(directory, [
                (0.9, 1.0, 0), (0.8, 2.0, 0), (0.7, 3.0, 0), (0.6, 4.0, 0),
            ])
            output = Path(directory) / "split"
            split_priority_bundle(source, output, importance="scale")
            self.assertEqual(list(read_bundle(output / "high-priority.bundle")), [records[2], records[3]])

    def test_odd_object_count_is_lossless_and_reports_byte_splat_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _ = self.make_source(directory, [
                (0.1, 1.0, 0), (0.2, 1.0, 1), (0.3, 1.0, 2),
                (0.4, 1.0, 2), (0.5, 1.0, 2),
            ])
            output = Path(directory) / "split"
            manifest = split_priority_bundle(source, output, importance="native-tier")
            self.assertEqual(manifest["high"]["objects"], 2)
            self.assertEqual(manifest["low"]["objects"], 3)
            self.assertEqual(manifest["high"]["objects"] + manifest["low"]["objects"], 5)
            self.assertIn("byte_fraction_high", manifest["balance"])
            self.assertIn("gaussian_fraction_high", manifest["balance"])
            on_disk = json.loads((output / "priority-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["schema_version"], 2)

    def test_duplicate_embedded_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            duplicate = frame([0.5], [(1.0, 1.0, 1.0)], object_id=0, subgroup_id=0)
            source = Path(directory) / "scene.bundle"
            write_bundle(source, [duplicate, duplicate])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                split_priority_bundle(source, Path(directory) / "split")


if __name__ == "__main__":
    unittest.main()
