from __future__ import annotations

import ipaddress
import struct
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

from analyze_3dgs_reordering import (
    PRAGUE_PORT,
    RENO_PORT,
    Packet,
    analyze_packet_order,
    evaluate_acceptance,
    read_capture,
    read_packet_log,
)
from capture_udp_order import parse_udp_packet
from reordering_workload import (
    manifest_importance_ranks,
    reorder_bundle_by_track_order,
    split_by_l4s_fraction,
    validate_admission_order,
    validate_cross_path_admission_order,
    write_trace_release_schedule,
)
from render_3dgs_reordering import (
    gif_frame_durations_ms,
    quantize_gif_durations_ms,
)
from run_qemu_3dgs_reordering import (
    _background_rate,
    _fractions as qemu_fractions,
    _iperf_background_command,
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
    def test_qemu_wrapper_accepts_endpoint_fractions(self):
        self.assertEqual(qemu_fractions("0,0.25,1"), "0,0.25,1")

    def test_unlimited_background_omits_rate_limit(self):
        self.assertIsNone(_background_rate("unlimited"))
        command = _iperf_background_command("10.0.0.3", 10.0, None, "reno")
        self.assertNotIn("-b", command)
        self.assertIn("-C", command)

    def test_background_rate_parser_preserves_numeric_and_disabled_modes(self):
        self.assertEqual(_background_rate("280"), 280.0)
        self.assertEqual(_background_rate("0"), 0.0)

    def test_endpoint_fractions_assign_the_complete_scene_to_one_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scene.bundle"
            records = [
                frame("track-a", opacity=0.9, gaussians=1),
                frame("track-b", opacity=0.8, gaussians=2),
            ]
            write_bundle(source, records)
            zero = split_by_l4s_fraction(
                source, root / "zero", l4s_fraction=0.0
            )
            one = split_by_l4s_fraction(
                source, root / "one", l4s_fraction=1.0
            )
        self.assertEqual(zero["high"]["objects"], 0)
        self.assertEqual(zero["low"]["objects"], len(records))
        self.assertEqual(zero["balance"]["actual_l4s_byte_fraction"], 0.0)
        self.assertEqual(one["high"]["objects"], len(records))
        self.assertEqual(one["low"]["objects"], 0)
        self.assertEqual(one["balance"]["actual_l4s_byte_fraction"], 1.0)

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

    def test_trace_release_schedule_records_eligibility_and_global_rank(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "ordered.bundle"
            records = [
                frame("track-a", opacity=0.2, object_id=0),
                frame(
                    "track-a", opacity=0.1, subgroup_id=1, object_id=1
                ),
                frame("track-b", opacity=0.9, object_id=2),
                frame(
                    "track-c", opacity=0.8, subgroup_id=2, object_id=3
                ),
            ]
            write_bundle(
                bundle,
                records,
            )
            manifest = split_by_l4s_fraction(
                bundle, root / "split", l4s_fraction=1.0, importance="opacity"
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
                importance_ranks=manifest_importance_ranks(manifest),
            )
            rows = [
                tuple(map(int, line.split()))
                for line in schedule.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows, [(5, 2), (5, 3), (45, 0), (145, 1)])
            self.assertEqual(result["records"], 4)
            self.assertEqual(result["format"], "release_ms importance_rank")
            self.assertEqual(result["viewport_gated_subgroups"], [0, 1, 2])

    def test_admission_log_proves_best_rank_among_eligible_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schedule = root / "schedule.txt"
            schedule.write_text("0 5\n10 0\n0 2\n", encoding="utf-8")
            admission = root / "admission.csv"
            admission.write_text(
                "admission_index,time_us,bundle_record_index,release_ms,"
                "importance_rank,subgroup_id,payload_bytes\n"
                "0,100,2,0,2,0,100\n"
                "1,200,0,0,5,1,200\n"
                "2,10000,1,10,0,0,100\n",
                encoding="utf-8",
            )
            result = validate_admission_order(schedule, admission)
            self.assertTrue(result["validated"])
            self.assertEqual(result["admitted_records"], 3)

    def test_cross_path_admission_allows_classic_while_prague_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prague = root / "prague.txt"
            classic = root / "classic.txt"
            combined = root / "combined.csv"
            prague.write_text("10 0\n", encoding="utf-8")
            classic.write_text("0 1\n", encoding="utf-8")
            combined.write_text(
                "admission_index,time_us,path,bundle_record_index,release_ms,"
                "importance_rank,subgroup_id,payload_bytes,"
                "queued_stream_bytes_before,bytes_in_flight_before,"
                "cwnd_bytes_before,queue_threshold_bytes\n"
                "0,100,low-reno,0,0,1,1,100,0,0,1000,100\n"
                "1,10000,high-prague,0,10,0,0,100,0,0,1000,100\n",
                encoding="utf-8",
            )
            result = validate_cross_path_admission_order(prague, classic, combined)
            self.assertTrue(result["validated"])

    def test_cross_path_admission_accepts_deadline_truncated_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prague = root / "prague.txt"
            classic = root / "classic.txt"
            combined = root / "combined.csv"
            prague.write_text("100 0\n", encoding="utf-8")
            classic.write_text("0 1\n0 2\n", encoding="utf-8")
            combined.write_text(
                "admission_index,time_us,path,bundle_record_index,release_ms,"
                "importance_rank,subgroup_id,payload_bytes,"
                "queued_stream_bytes_before,bytes_in_flight_before,"
                "cwnd_bytes_before,queue_threshold_bytes\n"
                "0,100,low-reno,0,0,1,1,100,0,0,1000,100\n",
                encoding="utf-8",
            )
            result = validate_cross_path_admission_order(prague, classic, combined)
            self.assertTrue(result["validated"])
            self.assertEqual(result["admitted_classic_records"], 1)

    def test_cross_path_admission_rejects_classic_while_prague_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prague = root / "prague.txt"
            classic = root / "classic.txt"
            combined = root / "combined.csv"
            prague.write_text("0 0\n", encoding="utf-8")
            classic.write_text("0 1\n", encoding="utf-8")
            combined.write_text(
                "admission_index,time_us,path,bundle_record_index,release_ms,"
                "importance_rank,subgroup_id,payload_bytes,"
                "queued_stream_bytes_before,bytes_in_flight_before,"
                "cwnd_bytes_before,queue_threshold_bytes\n"
                "0,100,low-reno,0,0,1,1,100,0,0,1000,100\n"
                "1,200,high-prague,0,0,0,0,100,0,0,1000,100\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Prague was available"):
                validate_cross_path_admission_order(prague, classic, combined)

    def test_gif_timing_preserves_sampled_trace_timestamps(self):
        frames = [
            {"timestamp_ms": 0.0},
            {"timestamp_ms": 20.0},
            {"timestamp_ms": 55.0},
            {"timestamp_ms": 90.0},
        ]
        self.assertEqual(
            gif_frame_durations_ms(frames, [0, 2, 3], None),
            [55, 35, 45],
        )
        self.assertEqual(
            gif_frame_durations_ms(frames, [0, 2, 3], 40),
            [40, 40, 40],
        )

    def test_gif_duration_quantization_does_not_accumulate_drift(self):
        durations = [28, 27, 28, 27, 27]
        quantized = quantize_gif_durations_ms(durations)
        self.assertEqual(quantized, [30, 30, 20, 30, 30])
        self.assertLessEqual(abs(sum(quantized) - sum(durations)), 5)


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

    def test_single_transport_endpoint_controls_do_not_require_inversions(self):
        no_drops = {
            "provider_ingress": {"socket_drops": 0},
            "provider_egress": {"socket_drops": 0},
            "downstream_egress": {"socket_drops": 0},
        }
        for fraction, port in ((0.0, RENO_PORT), (1.0, PRAGUE_PORT)):
            with self.subTest(fraction=fraction):
                packets = [self.packet(1.0, port, 1000, "only-flow")]
                result = analyze_packet_order(packets, packets, packets)
                acceptance = evaluate_acceptance(
                    result,
                    requested_l4s_fraction=fraction,
                    capture_stats=no_drops,
                    minimum_provider_match_fraction=0.95,
                    minimum_downstream_match_fraction=0.95,
                )
                self.assertEqual(acceptance["status"], "pass")
                self.assertFalse(acceptance["cross_class_reordering_applicable"])


    def test_reads_classic_pcap_without_tshark(self):
        payload = b"encrypted-quic-payload"
        udp = struct.pack("!HHHH", RENO_PORT, 5555, len(payload) + 8, 0) + payload
        ipv4 = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(udp),
            1,
            0,
            64,
            17,
            0,
            bytes((10, 0, 0, 1)),
            bytes((10, 0, 0, 2)),
        )
        ethernet = b"\0" * 12 + b"\x08\x00" + ipv4 + udp
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.pcap"
            capture.write_bytes(
                struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
                + struct.pack("<IIII", 123, 456000, len(ethernet), len(ethernet))
                + ethernet
            )
            packets = read_capture(capture)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].source_port, RENO_PORT)
        self.assertEqual(packets[0].payload_bytes, len(payload))
        self.assertAlmostEqual(packets[0].time_s, 123.456)

    def test_compact_packet_log_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packets.csv"
            path.write_text(
                "time_s,source_port,payload_bytes,fingerprint,occurrence\n"
                "123.456000000,4443,21,abc,0\n",
                encoding="utf-8",
            )
            packets = read_packet_log(path)
        self.assertEqual(packets, [self.packet(123.456, RENO_PORT, 21, "abc")])

    def test_compact_capture_parser_extracts_udp_identity(self):
        payload = b"encrypted-quic-payload"
        udp = struct.pack("!HHHH", PRAGUE_PORT, 5555, len(payload) + 8, 0) + payload
        ipv4 = struct.pack(
            "!BBHHHBBH4s4s",
            0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0,
            bytes((10, 0, 0, 1)), bytes((10, 0, 0, 2)),
        )
        ethernet = b"\0" * 12 + b"\x08\x00" + ipv4 + udp
        parsed = parse_udp_packet(
            ethernet, ipaddress.IPv4Address("10.0.0.1").packed
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], PRAGUE_PORT)
        self.assertEqual(parsed[1], len(payload))


if __name__ == "__main__":
    unittest.main()
