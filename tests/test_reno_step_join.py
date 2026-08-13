#!/usr/bin/env python3
"""Dependency-light tests for the ten-phase Reno step-join experiment."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

import analyze_reno_step_join as analyze
from reno_step_join_common import (
    PHASE_LABELS,
    STREAM_COUNT,
    STREAMS,
    build_iperf_client_command,
    phase_boundaries,
)
import run_reno_step_join as runner
import visualization


class FakeClock:
    def __init__(self, start=100.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.0001)


class FakeProcess:
    def __init__(self, clock, duration, status=0):
        self.clock = clock
        self.end = clock() + duration
        self.status = status

    def poll(self):
        return self.status if self.clock() >= self.end else None


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ScheduleContractTests(unittest.TestCase):
    def test_fixed_ten_stream_schedule_has_unique_ports_and_common_endpoint(self):
        self.assertEqual(STREAM_COUNT, 10)
        self.assertEqual([stream.name for stream in STREAMS],
                         [f"stream_{index:02d}" for index in range(1, 11)])
        self.assertEqual([stream.start_phase for stream in STREAMS], list(range(10)))
        self.assertEqual([stream.start_seconds(5) for stream in STREAMS],
                         list(range(0, 50, 5)))
        self.assertEqual([stream.duration_seconds(5) for stream in STREAMS],
                         list(range(50, 0, -5)))
        self.assertEqual({stream.end_seconds(5) for stream in STREAMS}, {50})
        self.assertEqual(len({stream.server_port for stream in STREAMS}), 10)
        self.assertEqual(len({stream.client_port for stream in STREAMS}), 10)
        self.assertEqual(phase_boundaries(5), list(range(0, 55, 5)))

    def test_each_client_is_one_unlimited_reno_connection(self):
        for stream in STREAMS:
            command = build_iperf_client_command("10.0.0.2", stream, 5)
            self.assertEqual(command[command.index("-C") + 1], "reno")
            self.assertEqual(command[command.index("--cport") + 1], str(stream.client_port))
            self.assertEqual(command[command.index("-p") + 1], str(stream.server_port))
            self.assertNotIn("-P", command)
            self.assertNotIn("-b", command)
            self.assertIn("-J", command)

    def test_fake_clock_launches_without_drift_and_all_streams_reach_endpoint(self):
        clock = FakeClock()
        launched = []

        def launch(stream):
            launched.append((stream.name, clock()))
            return FakeProcess(clock, stream.duration_seconds(5))

        events, processes = runner.execute_stream_schedule(
            5, launch, clock=clock, sleeper=clock.sleep, origin=100.0
        )
        starts = [row for row in events if row["event"] == "start"]
        ends = [row for row in events if row["event"] == "end"]
        self.assertEqual([round(row["time_s"], 6) for row in starts],
                         [float(value) for value in range(0, 50, 5)])
        self.assertTrue(all(math.isclose(row["time_s"], 50, abs_tol=0.051) for row in ends))
        self.assertEqual(set(processes), {stream.name for stream in STREAMS})
        runner.validate_schedule_events(events, 5)

    def test_nonzero_and_early_completion_are_rejected(self):
        clock = FakeClock()

        def failed_launch(stream):
            status = 7 if stream.name == "stream_04" else 0
            return FakeProcess(clock, stream.duration_seconds(1), status)

        events, _ = runner.execute_stream_schedule(
            1, failed_launch, clock=clock, sleeper=clock.sleep, origin=100.0
        )
        with self.assertRaisesRegex(RuntimeError, "failed"):
            runner.validate_schedule_events(events, 1)

        clean = [dict(row) for row in events]
        for row in clean:
            if row["event"] == "end":
                row["status"] = 0
        next(row for row in clean
             if row["stream"] == "stream_01" and row["event"] == "end")["time_s"] = 8
        with self.assertRaisesRegex(RuntimeError, "before"):
            runner.validate_schedule_events(clean, 1)


class AnalysisTests(unittest.TestCase):
    def metadata(self):
        return {
            "phase_seconds": 1.0,
            "server_ip": "10.0.0.2",
            "experiment_started_epoch": 1000.0,
            "streams": [
                {
                    "stream": stream.name,
                    "client_port": stream.client_port,
                    "server_port": stream.server_port,
                }
                for stream in STREAMS
            ],
        }

    def test_pcap_parser_attributes_all_unique_client_ports_and_preserves_ecn(self):
        output = "\n".join(
            f"{1000 + index / 10:.3f}\t1500\t{stream.client_port}\t0x{index % 4:x}"
            for index, stream in enumerate(STREAMS)
        ) + "\n"
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return Completed(stdout=output)

        packets = analyze.parse_pcap(Path("capture.pcap"), self.metadata(), runner=fake_run)
        self.assertEqual([packet.instance for packet in packets],
                         [stream.name for stream in STREAMS])
        self.assertEqual([packet.ecn for packet in packets], [index % 4 for index in range(10)])
        display_filter = calls[0][calls[0].index("-Y") + 1]
        for stream in STREAMS:
            self.assertIn(f"tcp.srcport == {stream.client_port}", display_filter)

    def test_timeline_clips_packets_and_carries_latest_cwnd_only_while_active(self):
        metadata = self.metadata()
        packets = [
            analyze.PacketSample(0.10, 125000, "stream_01", 0, False),
            analyze.PacketSample(10.10, 125000, "stream_01", 0, False),
            analyze.PacketSample(1.10, 62500, "stream_02", 0, False),
        ]
        transports = {
            "stream_01": [analyze.TransportSample(0.10, 64000, 1, 1, 0)],
            "stream_02": [analyze.TransportSample(1.10, 32000, 1, 1, 0)],
        }
        rows = analyze.build_timeline(metadata, packets, transports, bin_seconds=0.25)
        first = next(row for row in rows
                     if row["stream"] == "stream_01" and math.isclose(row["time_s"], 0.125))
        inactive = next(row for row in rows
                        if row["stream"] == "stream_02" and math.isclose(row["time_s"], 0.125))
        second = next(row for row in rows
                      if row["stream"] == "stream_02" and math.isclose(row["time_s"], 1.125))
        self.assertEqual(first["throughput_mbps"], 4.0)
        self.assertEqual(first["cwnd_bytes"], 64000)
        self.assertEqual(inactive["active"], 0)
        self.assertIsNone(inactive["cwnd_bytes"])
        self.assertEqual(second["throughput_mbps"], 2.0)
        self.assertEqual(max(float(row["time_s"]) for row in rows), 9.875)

    def test_aggregate_uses_only_active_samples_and_sample_stdev(self):
        timelines = [analyze._synthetic_timeline(offset) for offset in (0, 1, 2)]
        rows = analyze.aggregate_timelines(timelines, 3)
        active = next(row for row in rows
                      if row["stream"] == "stream_01" and row["active_repetitions"] == 3)
        inactive = next(row for row in rows
                        if row["stream"] == "stream_10" and row["active_repetitions"] == 0)
        self.assertEqual(active["throughput_mbps_mean"], 2.0)
        self.assertEqual(active["throughput_mbps_stdev"], 1.0)
        self.assertIsNone(inactive["throughput_mbps_mean"])
        with self.assertRaises(analyze.AnalysisError):
            analyze.aggregate_timelines(timelines[:2], 3)
        broken = [dict(row) for row in timelines[2][1:]]
        with self.assertRaisesRegex(analyze.AnalysisError, "aligned"):
            analyze.aggregate_timelines((timelines[0], timelines[1], broken), 3)


class VisualizationTests(unittest.TestCase):
    def test_per_run_and_aggregate_two_panel_plots_render(self):
        timelines = [analyze._synthetic_timeline(offset) for offset in (0, 1, 2)]
        aggregate = analyze.aggregate_timelines(timelines, 3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            visualization.plot_step_join_timeline(
                timelines[0], phase_boundaries(1), PHASE_LABELS, 20,
                root / "timeline", run_label="rep_001",
            )
            visualization.plot_step_join_timeline(
                aggregate, phase_boundaries(1), PHASE_LABELS, 20,
                root / "aggregate", aggregate=True,
            )
            for name in ("timeline", "aggregate"):
                svg, png = root / f"{name}.svg", root / f"{name}.png"
                self.assertGreater(svg.stat().st_size, 1000)
                self.assertGreater(png.stat().st_size, 1000)
                self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
