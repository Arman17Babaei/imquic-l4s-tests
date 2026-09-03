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
from reordering_workload_v2 import split_l4s_spectrum
from split_3dgs_priority import object_identity
from three_dgs_bundle import read_bundle, write_bundle


def _array(values):
    encoded = struct.pack(f"<{len(values)}f", *values)
    return struct.pack("<I", len(encoded)) + encoded


def frame(
    track: str,
    subgroup: int,
    object_id: int,
    opacity: float,
    gaussians: int = 1,
):
    track_bytes = track.encode()
    group = b"0"
    means = [0.0] * (gaussians * 3)
    opacities = [opacity] * gaussians
    sh = [0.0] * (gaussians * 3)
    scales = [0.0] * (gaussians * 3)
    rotations = [0.0] * (gaussians * 4)
    body = (
        track_bytes
        + group
        + _array(means)
        + _array(opacities)
        + _array(sh)
        + _array(scales)
        + _array(rotations)
    )
    return struct.pack(
        "<8I",
        0x47535033,
        1,
        len(track_bytes),
        len(group),
        object_id,
        gaussians,
        len(body),
        subgroup,
    ) + body


def subgroups(path: Path) -> list[int]:
    return [
        int(object_identity(payload)["subgroup_id"])
        for payload in read_bundle(path)
    ]


class SpectrumSplitTests(unittest.TestCase):
    def make_scene(self, root: Path) -> Path:
        source = root / "scene.bundle"
        write_bundle(
            source,
            [
                frame("track-a", 0, 0, 0.20, 1),
                frame("track-a", 0, 1, 0.90, 1),
                frame("track-b", 0, 2, 0.50, 1),
                frame("track-a", 1, 3, 0.95, 2),
                frame("track-b", 1, 4, 0.40, 2),
                frame("track-b", 2, 5, 0.99, 3),
            ],
        )
        return source

    def test_rank_is_layer_then_mean_opacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = split_l4s_spectrum(
                self.make_scene(root),
                root / "split",
                l4s_fraction=0.5,
            )
            ranked = sorted(
                manifest["objects"],
                key=lambda row: int(row["importance_rank"]),
            )
            self.assertEqual(
                [(int(row["layer"]), round(float(row["mean_opacity"]), 2)) for row in ranked],
                [(0, 0.90), (0, 0.50), (0, 0.20), (1, 0.95), (1, 0.40), (2, 0.99)],
            )

    def test_endpoints_keep_two_transport_bundles_but_move_all_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_scene(root)
            all_reno = split_l4s_spectrum(
                source, root / "all-reno", l4s_fraction=0.0
            )
            all_prague = split_l4s_spectrum(
                source, root / "all-prague", l4s_fraction=1.0
            )
            self.assertEqual(all_reno["prague"]["objects"], 0)
            self.assertEqual(all_reno["reno"]["objects"], all_reno["source_objects"])
            self.assertEqual(all_prague["reno"]["objects"], 0)
            self.assertEqual(
                all_prague["prague"]["objects"], all_prague["source_objects"]
            )
            self.assertEqual(all_reno["actual_l4s_byte_fraction"], 0.0)
            self.assertEqual(all_prague["actual_l4s_byte_fraction"], 1.0)
            self.assertEqual(all_reno["prague"]["port"], 4444)
            self.assertEqual(all_reno["reno"]["port"], 4443)

    def test_small_fraction_can_split_inside_layer_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = split_l4s_spectrum(
                self.make_scene(root),
                root / "split",
                l4s_fraction=0.20,
            )
            prague_rows = [
                row for row in manifest["objects"] if row["path"] == "high-prague"
            ]
            reno_rows = [
                row for row in manifest["objects"] if row["path"] == "low-reno"
            ]
            self.assertTrue(prague_rows)
            self.assertTrue(reno_rows)
            self.assertTrue(any(int(row["layer"]) == 0 for row in reno_rows))
            self.assertLess(
                max(int(row["importance_rank"]) for row in prague_rows),
                min(int(row["importance_rank"]) for row in reno_rows),
            )

    def test_intermediate_fraction_preserves_every_object_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_scene(root)
            manifest = split_l4s_spectrum(
                source, root / "split", l4s_fraction=0.5
            )
            self.assertEqual(
                int(manifest["prague"]["objects"]) + int(manifest["reno"]["objects"]),
                manifest["source_objects"],
            )
            self.assertEqual(
                int(manifest["prague"]["payload_bytes"])
                + int(manifest["reno"]["payload_bytes"]),
                manifest["source_payload_bytes"],
            )
            identities = {
                (
                    row["track_id"],
                    row["group_id"],
                    int(row["subgroup_id"]),
                    int(row["object_id"]),
                )
                for row in manifest["objects"]
            }
            self.assertEqual(len(identities), manifest["source_objects"])


class NegativeResultValidityTests(unittest.TestCase):
    @staticmethod
    def packet(time_s: float, port: int, fingerprint: str) -> Packet:
        return Packet(time_s, port, 1000, fingerprint, 0)

    def test_overlap_without_overtake_is_a_valid_negative_result(self):
        ingress = [
            self.packet(1.0, RENO_PORT, "r"),
            self.packet(2.0, PRAGUE_PORT, "p"),
        ]
        egress = [
            self.packet(3.0, RENO_PORT, "r"),
            self.packet(4.0, PRAGUE_PORT, "p"),
        ]
        result = analyze_packet_order(ingress, egress, egress)
        self.assertEqual(result["provider_opportunity"]["opportunity_pairs"], 1)
        self.assertEqual(result["provider_reordering"]["inversion_pairs"], 0)
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
        self.assertTrue(acceptance["opportunity_observed"])
        self.assertFalse(acceptance["mechanism_observed"])
        self.assertEqual(
            acceptance["hypothesis_outcome"],
            "provider overlap existed but no reordering observed",
        )

    def test_no_overlap_is_distinct_from_failed_overtaking(self):
        ingress = [
            self.packet(1.0, RENO_PORT, "r"),
            self.packet(4.0, PRAGUE_PORT, "p"),
        ]
        provider = [
            self.packet(2.0, RENO_PORT, "r"),
            self.packet(5.0, PRAGUE_PORT, "p"),
        ]
        result = analyze_packet_order(ingress, provider, provider)
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
        self.assertFalse(acceptance["opportunity_observed"])
        self.assertEqual(
            acceptance["hypothesis_outcome"],
            "no provider overlap opportunity observed",
        )


if __name__ == "__main__":
    unittest.main()
