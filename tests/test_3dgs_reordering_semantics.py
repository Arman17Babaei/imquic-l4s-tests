from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from analyze_3dgs_reordering import (
    Packet,
    PRAGUE_PORT,
    RENO_PORT,
    analyze_packet_order,
    evaluate_acceptance,
)
from reordering_workload_v2 import split_base_enhancement
from split_3dgs_priority import object_identity
from three_dgs_bundle import read_bundle, write_bundle


def _array(values):
    encoded = struct.pack(f"<{len(values)}f", *values)
    return struct.pack("<I", len(encoded)) + encoded


def frame(track: str, subgroup: int, object_id: int, opacity: float, gaussians: int = 1):
    track_bytes = track.encode()
    group = b"0"
    means = [0.0] * (gaussians * 3)
    opacities = [opacity] * gaussians
    sh = [0.0] * (gaussians * 3)
    scales = [0.0] * (gaussians * 3)
    rotations = [0.0] * (gaussians * 4)
    body = (
        track_bytes + group + _array(means) + _array(opacities)
        + _array(sh) + _array(scales) + _array(rotations)
    )
    return struct.pack(
        "<8I", 0x47535033, 1, len(track_bytes), len(group), object_id,
        gaussians, len(body), subgroup,
    ) + body


def subgroups(path: Path) -> list[int]:
    return [int(object_identity(payload)["subgroup_id"]) for payload in read_bundle(path)]


class SemanticSplitTests(unittest.TestCase):
    def make_scene(self, root: Path) -> Path:
        source = root / "scene.bundle"
        write_bundle(
            source,
            [
                frame("track-a", 0, 0, 0.9, 1),
                frame("track-a", 1, 1, 0.8, 2),
                frame("track-a", 2, 2, 0.7, 3),
                frame("track-b", 0, 3, 0.6, 1),
                frame("track-b", 1, 4, 0.5, 4),
                frame("track-b", 2, 5, 0.4, 5),
            ],
        )
        return source

    def test_base_never_leaves_dedicated_prague_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_scene(root)
            for fraction in (0.0, 0.4, 1.0):
                with self.subTest(fraction=fraction):
                    manifest = split_base_enhancement(
                        source,
                        root / f"split-{fraction}",
                        enhancement_l4s_fraction=fraction,
                        importance="opacity",
                    )
                    base = Path(manifest["base"]["path"])
                    enh_l4s = Path(manifest["enhancement_l4s"]["path"])
                    enh_classic = Path(manifest["enhancement_classic"]["path"])
                    self.assertTrue(all(value == 0 for value in subgroups(base)))
                    self.assertTrue(all(value in (1, 2) for value in subgroups(enh_l4s)))
                    self.assertTrue(all(value in (1, 2) for value in subgroups(enh_classic)))
                    rows = manifest["objects"]
                    self.assertTrue(
                        all(
                            row["path"] == "high-prague"
                            for row in rows if int(row["subgroup_id"]) == 0
                        )
                    )

    def test_endpoints_keep_base_on_prague_and_change_only_enhancement_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_scene(root)
            mixed = split_base_enhancement(
                source, root / "mixed", enhancement_l4s_fraction=0.0,
                importance="opacity",
            )
            all_l4s = split_base_enhancement(
                source, root / "all-l4s", enhancement_l4s_fraction=1.0,
                importance="opacity",
            )

            self.assertGreater(mixed["base"]["objects"], 0)
            self.assertEqual(mixed["enhancement_l4s"]["objects"], 0)
            self.assertGreater(mixed["enhancement_classic"]["objects"], 0)

            self.assertGreater(all_l4s["base"]["objects"], 0)
            self.assertGreater(all_l4s["enhancement_l4s"]["objects"], 0)
            self.assertEqual(all_l4s["enhancement_classic"]["objects"], 0)

            self.assertEqual(
                mixed["base"]["payload_bytes"], all_l4s["base"]["payload_bytes"]
            )
            self.assertEqual(
                mixed["base"]["objects"], all_l4s["base"]["objects"]
            )
            self.assertEqual(mixed["actual_enhancement_l4s_fraction"], 0.0)
            self.assertEqual(all_l4s["actual_enhancement_l4s_fraction"], 1.0)

    def test_intermediate_fraction_preserves_every_object_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_scene(root)
            manifest = split_base_enhancement(
                source, root / "split", enhancement_l4s_fraction=0.5,
                importance="opacity",
            )
            total = sum(
                int(manifest[key]["objects"])
                for key in ("base", "enhancement_l4s", "enhancement_classic")
            )
            total_bytes = sum(
                int(manifest[key]["payload_bytes"])
                for key in ("base", "enhancement_l4s", "enhancement_classic")
            )
            self.assertEqual(total, manifest["source_objects"])
            self.assertEqual(total_bytes, manifest["source_payload_bytes"])


class NegativeResultValidityTests(unittest.TestCase):
    @staticmethod
    def packet(time_s: float, port: int, fingerprint: str) -> Packet:
        return Packet(time_s, port, 1000, fingerprint, 0)

    def test_zero_overtake_is_a_valid_negative_result(self):
        ingress = [
            self.packet(1.0, RENO_PORT, "r"),
            self.packet(2.0, PRAGUE_PORT, "p"),
        ]
        egress = [
            self.packet(3.0, RENO_PORT, "r"),
            self.packet(4.0, PRAGUE_PORT, "p"),
        ]
        result = analyze_packet_order(ingress, egress, egress)
        acceptance = evaluate_acceptance(
            result,
            requested_l4s_fraction=0.5,
            capture_stats={
                "provider_ingress": {"socket_drops": 0},
                "provider_egress": {"socket_drops": 0},
                "downstream_egress": {"socket_drops": 0},
            },
            minimum_provider_match_fraction=0.95,
            minimum_downstream_match_fraction=0.95,
        )
        self.assertEqual(acceptance["status"], "pass")
        self.assertFalse(acceptance["mechanism_observed"])
        self.assertEqual(
            acceptance["hypothesis_outcome"], "no provider reordering observed"
        )


if __name__ == "__main__":
    unittest.main()
