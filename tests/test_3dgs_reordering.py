from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from analyze_3dgs_reordering import PRAGUE_PORT, RENO_PORT, Packet, analyze_packet_order
from reordering_workload import (
    reorder_bundle_by_track_order,
    split_by_l4s_fraction,
    write_trace_release_schedule,
)
from split_3dgs_priority import object_identity
from three_dgs_bundle import read_bundle, write_bundle


def _array(values):
    if values is None:
        return struct.pack("<I", 0)
    encoded = struct.pack(f"<{len(values)}f", *values)
    return struct.pack("<I", len(encoded)) + encoded


def frame(
    track_id: str,
    *,
    opacity: float,
    subgroup_id: int = 0,
    object_id: int = 0,
    gaussians: int = 1,
):
    track = track_id.encode()
    group = b"0"
    means = [0.0] * (gaussians * 3)
    opacities = [opacity] * gaussians
    sh = [0.0] * (gaussians * 3)
    scales = [0.0] * (gaussians * 3)
    rotations = [0.0] * (gaussians * 4)
    body = track + group + _array(means) + _array(opacities) + _array(sh) + _array(scales) + _array(rotations)
    return struct.pack(
        "<8I",
        0x47535033,
        1,
        len(track),
        len(group),
        object_id,
        gaussians,
        len(body),
        subgroup_id,
    ) + body


class ReorderingWorkloadTests(unittest.TestCase):
    def test_l4s_fraction_is_payload_byte_target_with_whole_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scene.bundle"
            records = [
                frame("track-a", opacity=0.9, gaussians=1),
                frame("track-b", opacity=0.8, gaussians=2),
                frame("track-c", opacity=0.7, gaussians=4),
                frame("track-d", opacity=0.6, gaussians=8),
            ]
            write_bundle(source, records)
            manifest = split_by_l4s_fraction(
                source, root / "split", l4s_fraction=0.40, importance="opacity"
            )

            ranked = sorted(manifest["objects"], key=lambda row: int(row["importance_rank"]))
            prefix_bytes = []
            running = 0
            for row in ranked:
                running += int(row["payload_bytes"])
                prefix_bytes.append(running)
            target = int(manifest["source_payload_bytes"]) * 0.40
            high_bytes = int(manifest["high"]["payload_bytes"])
            legal = prefix_bytes[:-1]
            self.assertEqual(
                abs(high_bytes - target),
                min(abs(value - target) for value in legal),
            )
            self.assertGreater(manifest["high"]["objects"], 0)
            self.assertGreater(manifest["low"]["objects"], 0)
            self.assertEqual(
                manifest["high"]["objects"] + manifest["low"]["objects"],
                len(records),
            )

    def test_high_bundle_can_be_frozen_in_track_demand_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bundle"
            records = [
                frame("track-c", opacity=0.5, object_id=0),
                frame("track-a", opacity=0.4, object_id=1),
                frame("track-b", opacity=0.3, object_id=2),
                frame("track-a", opacity=0.2, object_id=3),
            ]
            write_bundle(source, records)
            destination = root / "ordered.bundle"
            reorder_bundle_by_track_order(
                source, destination, ["track-a", "track-b", "track-c"]
            )
            tracks = [
                str(object_identity(payload)["track_id"])
                for payload in read_bundle(destination)
            ]
            self.assertEqual(tracks, ["track-a", "track-a", "track-b", "track-c"])

    def test_trace_release_schedule_is_frozen_and_monotone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "ordered.bundle"
            write_bundle(
                bundle,
                [
                    frame("track-a", opacity=0.5, object_id=0),
                    frame("track-a", opacity=0.4, object_id=1),
                    frame("track-b", opacity=0.3, object_id=2),
                    frame("track-c", opacity=0.2, object_id=3),
                ],
            )
            schedule = root / "release.txt"
            result = write_trace_release_schedule(
                bundle,
                schedule,
                frozen_demand={
                    "track_order": ["track-a", "track-b", "track-c"],
                    "events": [
                        {"track_id": "track-a", "timestamp_ms": 0.0},
                        {"track_id": "track-b", "timestamp_ms": 20.0},
                    ],
                },
                initial_release_ms=5.0,
                time_scale=2.0,
                fallback_spacing_ms=100.0,
            )
            rows = [int(line) for line in schedule.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows, [5, 5, 45, 145])
            self.assertEqual(result["records"], 4)


class PacketReorderingAnalysisTests(unittest.TestCase):
    @staticmethod
    def packet(time_s, port, payload, fingerprint):
        return Packet(
            time_s=time_s,
            source_port=port,
            payload_bytes=payload,
            fingerprint=fingerprint,
            occurrence=0,
        )

    def test_detects_provider_overtake_and_downstream_persistence(self):
        ingress = [
            self.packet(1.0, RENO_PORT, 1000, "r1"),
            self.packet(1.5, RENO_PORT, 900, "r2"),
            self.packet(2.0, PRAGUE_PORT, 500, "p1"),
            self.packet(5.0, PRAGUE_PORT, 500, "p2"),
        ]
        provider = [
            self.packet(2.5, RENO_PORT, 900, "r2"),
            self.packet(3.0, PRAGUE_PORT, 500, "p1"),
            self.packet(4.0, RENO_PORT, 1000, "r1"),
            self.packet(6.0, PRAGUE_PORT, 500, "p2"),
        ]
        downstream = [
            self.packet(9.0, RENO_PORT, 900, "r2"),
            self.packet(10.0, PRAGUE_PORT, 500, "p1"),
            self.packet(20.0, RENO_PORT, 1000, "r1"),
            self.packet(21.0, PRAGUE_PORT, 500, "p2"),
        ]
        result = analyze_packet_order(ingress, provider, downstream)
        reorder = result["provider_reordering"]
        persistence = result["downstream_persistence"]
        self.assertEqual(reorder["inversion_pairs"], 1)
        self.assertEqual(reorder["unique_reno_packets_overtaken"], 1)
        self.assertEqual(reorder["unique_reno_payload_bytes_overtaken"], 1000)
        self.assertEqual(reorder["prague_packets_with_overtake"], 1)
        self.assertEqual(persistence["witness_pairs_preserved"], 1)
        self.assertEqual(persistence["preservation_fraction"], 1.0)
        self.assertGreater(persistence["gap_amplification_ratio"]["p50"], 1.0)

    def test_no_inversion_when_fifo_order_is_unchanged(self):
        ingress = [
            self.packet(1.0, RENO_PORT, 1000, "r"),
            self.packet(2.0, PRAGUE_PORT, 500, "p"),
        ]
        provider = [
            self.packet(3.0, RENO_PORT, 1000, "r"),
            self.packet(4.0, PRAGUE_PORT, 500, "p"),
        ]
        result = analyze_packet_order(ingress, provider, provider)
        self.assertEqual(result["provider_reordering"]["inversion_pairs"], 0)
        self.assertEqual(result["provider_reordering"]["unique_reno_payload_bytes_overtaken"], 0)


if __name__ == "__main__":
    unittest.main()
