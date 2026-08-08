#!/usr/bin/env python3
"""Documentation-backed tests for the Reno/Reno fairness harness.

Contracts encoded here come from:
- ESnet iperf3 manual: -C, -J, --cport, one-second -i default semantics,
  and unlimited TCP bitrate when -b is omitted.
- ESnet iperf3 JSON/Linux output: snd_cwnd, rtt, rttvar, retransmits and
  sender_tcp_congestion.
- iproute2 tc-htb/tc-pfifo manuals: HTB rate/ceil/burst/cburst and packet-
  counted pfifo limit.
- Wireshark TShark manual: -T fields with repeated -e fields and tab separator.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "l4s"
sys.path.insert(0, str(TOOLS))

import analyze_reno_fairness as analyze
from reno_fairness_common import (
    FLOW_INSTANCES,
    build_iperf_client_command,
    build_iperf_server_command,
    convergence_time,
    jain_fairness,
    parse_tc_rate_mbps,
    phase_boundaries,
    qdisc_commands,
)
import run_reno_fairness as runner
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
        self.signals = []
        self.killed = False

    def poll(self):
        return self.status if self.clock() >= self.end else None

    def send_signal(self, signal):
        self.signals.append(signal)

    def wait(self, timeout=None):
        if self.poll() is None:
            raise AssertionError("fake process waited before completion")
        return self.status

    def kill(self):
        self.killed = True


class FakeHost:
    def __init__(self, ip):
        self._ip = ip
        self.commands = []

    def IP(self):
        return self._ip

    def cmd(self, command):
        self.commands.append(command)
        return ""


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class CommandContractTests(unittest.TestCase):
    def test_five_phase_process_schedule_is_palindromic(self):
        self.assertEqual(
            [(f.instance, f.start_phase, f.duration_phases) for f in FLOW_INSTANCES],
            [("A1", 0, 2), ("B1", 1, 3), ("A2", 3, 2)],
        )
        self.assertEqual(phase_boundaries(15), [0, 15, 30, 45, 60, 75])

    def test_iperf_client_is_single_stream_unlimited_reno_json(self):
        flow = FLOW_INSTANCES[0]
        command = build_iperf_client_command("10.0.0.2", flow, 15)
        self.assertEqual(command[:3], ["iperf3", "-c", "10.0.0.2"])
        self.assertIn("-C", command)
        self.assertEqual(command[command.index("-C") + 1], "reno")
        self.assertIn("-J", command)
        self.assertIn("--cport", command)
        self.assertEqual(command[command.index("--cport") + 1], "6001")
        self.assertEqual(command[command.index("-t") + 1], "30")
        self.assertNotIn("-b", command)  # documented unlimited default for TCP
        self.assertNotIn("-P", command)  # documented default is one stream

    def test_iperf_server_ports_are_stable_per_logical_flow(self):
        self.assertEqual(build_iperf_server_command(5201), ["iperf3", "-s", "-p", "5201"])
        self.assertEqual(build_iperf_server_command(5202), ["iperf3", "-s", "-p", "5202"])

    def test_qdisc_matches_documented_htb_and_packet_fifo_contract(self):
        commands = qdisc_commands("s1-eth2", "20mbit", 10000)
        self.assertEqual(commands[1], [
            "tc", "qdisc", "add", "dev", "s1-eth2", "root", "handle", "1:",
            "htb", "default", "1",
        ])
        self.assertEqual(commands[2], [
            "tc", "class", "add", "dev", "s1-eth2", "parent", "1:",
            "classid", "1:1", "htb", "rate", "20mbit", "ceil", "20mbit",
            "burst", "32k", "cburst", "1600",
        ])
        self.assertEqual(commands[3], [
            "tc", "qdisc", "add", "dev", "s1-eth2", "parent", "1:1",
            "handle", "10:", "pfifo", "limit", "10000",
        ])

    def test_rate_parser_accepts_tc_units_and_rejects_ambiguous_input(self):
        self.assertEqual(parse_tc_rate_mbps("20mbit"), 20.0)
        self.assertEqual(parse_tc_rate_mbps("3gbit"), 3000.0)
        self.assertEqual(parse_tc_rate_mbps("500kbit"), 0.5)
        with self.assertRaises(ValueError):
            parse_tc_rate_mbps("20M")

    def test_not_ect_configuration_covers_all_tcp_between_hosts(self):
        client, server = FakeHost("10.0.0.1"), FakeHost("10.0.0.2")
        runner.configure_not_ect(client, server)
        self.assertIn("sysctl -qw net.ipv4.tcp_ecn=0", client.commands)
        self.assertIn("sysctl -qw net.ipv4.tcp_ecn=0", server.commands)
        self.assertTrue(any("-d 10.0.0.2" in command and "--set-tos 0x00" in command
                            for command in client.commands))
        self.assertTrue(any("-d 10.0.0.1" in command and "--set-tos 0x00" in command
                            for command in server.commands))

    @mock.patch("run_reno_fairness.subprocess.run")
    def test_qdisc_runner_ignores_only_delete_failure(self, run):
        run.return_value = Completed()
        runner.configure_fifo_bottleneck("s1-eth2", "20mbit", 10000)
        self.assertEqual(run.call_count, 4)
        self.assertFalse(run.call_args_list[0].kwargs["check"])
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list[1:]))


class SchedulerTests(unittest.TestCase):
    def test_mocked_scheduler_hits_all_start_and_end_phases_without_drift(self):
        clock = FakeClock()
        launched = []

        def launch(flow):
            launched.append((flow.instance, clock()))
            return FakeProcess(clock, flow.duration_seconds(10))

        events, processes = runner.execute_flow_schedule(
            10,
            launch,
            clock=clock,
            sleeper=clock.sleep,
            poll_interval=0.05,
            origin=100.0,
        )
        starts = {row["instance"]: row["time_s"] for row in events if row["event"] == "start"}
        ends = {row["instance"]: row["time_s"] for row in events if row["event"] == "end"}
        self.assertAlmostEqual(starts["A1"], 0, places=6)
        self.assertAlmostEqual(starts["B1"], 10, places=6)
        self.assertAlmostEqual(starts["A2"], 30, places=6)
        self.assertAlmostEqual(ends["A1"], 20, delta=0.051)
        self.assertAlmostEqual(ends["B1"], 40, delta=0.051)
        self.assertAlmostEqual(ends["A2"], 50, delta=0.051)
        self.assertEqual(set(processes), {"A1", "B1", "A2"})

    def test_mocked_scheduler_preserves_nonzero_process_status(self):
        clock = FakeClock()

        def launch(flow):
            status = 7 if flow.instance == "B1" else 0
            return FakeProcess(clock, flow.duration_seconds(1), status=status)

        events, _ = runner.execute_flow_schedule(
            1, launch, clock=clock, sleeper=clock.sleep, origin=100.0
        )
        statuses = {row["instance"]: row["status"] for row in events if row["event"] == "end"}
        self.assertEqual(statuses["B1"], 7)


class IperfJsonTests(unittest.TestCase):
    def fixture(self, client_port=6001, server_port=5201, congestion="reno",
                target_bitrate=0, include_cwnd=True):
        stream = {
            "socket": 5,
            "start": 0,
            "end": 1.0,
            "seconds": 1.0,
            "bytes": 1_000_000,
            "bits_per_second": 8_000_000,
            "retransmits": 2,
            "rtt": 15000,
            "rttvar": 1500,
            "sender": True,
        }
        if include_cwnd:
            stream["snd_cwnd"] = 65536
        return {
            "start": {
                "connected": [{
                    "socket": 5,
                    "local_host": "10.0.0.1",
                    "local_port": client_port,
                    "remote_host": "10.0.0.2",
                    "remote_port": server_port,
                }],
                "test_start": {
                    "protocol": "TCP",
                    "num_streams": 1,
                    "duration": 30,
                    "target_bitrate": target_bitrate,
                },
            },
            "intervals": [{"streams": [stream], "sum": {"start": 0, "end": 1.0}}],
            "end": {"sender_tcp_congestion": congestion},
        }

    def write_fixture(self, directory, **kwargs):
        path = Path(directory) / "iperf.json"
        path.write_text(json.dumps(self.fixture(**kwargs)), encoding="utf-8")
        return path

    def test_documented_linux_tcp_info_fields_are_aligned_to_experiment_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_fixture(directory)
            samples = analyze.parse_iperf_json(
                path, {"client_port": 6001, "server_port": 5201}, actual_start_s=10.25
            )
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0].time_s, 11.25)
        self.assertEqual(samples[0].cwnd_bytes, 65536)
        self.assertEqual(samples[0].rtt_ms, 15.0)
        self.assertEqual(samples[0].rttvar_ms, 1.5)
        self.assertEqual(samples[0].retransmits, 2)

    def test_wrong_congestion_controller_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_fixture(directory, congestion="cubic")
            with self.assertRaisesRegex(analyze.AnalysisError, "expected 'reno'"):
                analyze.parse_iperf_json(path, {"client_port": 6001, "server_port": 5201}, 0)

    def test_rate_limited_tcp_fixture_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_fixture(directory, target_bitrate=10_000_000)
            with self.assertRaisesRegex(analyze.AnalysisError, "rate-limited"):
                analyze.parse_iperf_json(path, {"client_port": 6001, "server_port": 5201}, 0)

    def test_data_port_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_fixture(directory, client_port=65000)
            with self.assertRaisesRegex(analyze.AnalysisError, "data cport"):
                analyze.parse_iperf_json(path, {"client_port": 6001, "server_port": 5201}, 0)

    def test_missing_tcp_info_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_fixture(directory, include_cwnd=False)
            with self.assertRaisesRegex(analyze.AnalysisError, "TCP_INFO"):
                analyze.parse_iperf_json(path, {"client_port": 6001, "server_port": 5201}, 0)


class TsharkContractTests(unittest.TestCase):
    def metadata(self):
        return {
            "server_ip": "10.0.0.2",
            "experiment_started_epoch": 1000.0,
            "flows": [
                {"instance": "A1", "client_port": 6001},
                {"instance": "B1", "client_port": 6002},
                {"instance": "A2", "client_port": 6003},
            ],
        }

    def test_tshark_tab_fields_are_parsed_and_data_ports_attribute_instances(self):
        calls = []
        output = (
            "1000.125\t1500\t6001\t5201\t0x0\t\t\n"
            "1001.250\t1400\t6002\t5202\t0x0\t1\t\n"
            "1003.500\t1300\t6003\t5201\t0x0\t\t1\n"
        )

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return Completed(stdout=output)

        samples = analyze.parse_pcap(Path("capture.pcap"), self.metadata(), runner=fake_run)
        self.assertEqual([sample.instance for sample in samples], ["A1", "B1", "A2"])
        self.assertEqual([sample.ip_bytes for sample in samples], [1500, 1400, 1300])
        self.assertEqual([sample.retransmission for sample in samples], [False, True, True])
        args = calls[0][0]
        self.assertIn("-T", args)
        self.assertIn("fields", args)
        self.assertEqual(args.count("-e"), 7)
        filter_text = args[args.index("-Y") + 1]
        self.assertIn("ip.dst == 10.0.0.2", filter_text)
        self.assertIn("tcp.srcport == 6001", filter_text)
        self.assertIn("tcp.srcport == 6002", filter_text)
        self.assertIn("tcp.srcport == 6003", filter_text)
        self.assertIn("occurrence=f", args)

    def test_ecn_value_is_preserved_for_not_ect_validation(self):
        output = "1000.125\t1500\t6001\t5201\t0x3\t\t\n"
        samples = analyze.parse_pcap(
            Path("capture.pcap"), self.metadata(),
            runner=lambda *args, **kwargs: Completed(stdout=output),
        )
        self.assertEqual(samples[0].ecn, 3)

    def test_truncated_capture_return_code_two_is_accepted_only_with_tshark_warning(self):
        output = "1000.125\t1500\t6001\t5201\t0x0\t\t\n"
        result = lambda *args, **kwargs: Completed(
            stdout=output,
            stderr="file appears to have been cut short in the middle of a packet",
            returncode=2,
        )
        self.assertEqual(len(analyze.parse_pcap(Path("capture.pcap"), self.metadata(), runner=result)), 1)
        with self.assertRaises(analyze.AnalysisError):
            analyze.parse_pcap(
                Path("capture.pcap"), self.metadata(),
                runner=lambda *args, **kwargs: Completed(stderr="bad filter", returncode=2),
            )


class AnalysisMechanismTests(unittest.TestCase):
    def test_perfect_symmetry_has_zero_identity_and_incumbency_effect(self):
        rows, boundaries = analyze._synthetic_rows()
        summary = analyze.compute_summary_from_rows(rows, boundaries)
        self.assertAlmostEqual(summary["phase2_A_share"], 0.5)
        self.assertAlmostEqual(summary["phase4_A_share"], 0.5)
        self.assertAlmostEqual(summary["incumbency_effect"], 0.0)
        self.assertAlmostEqual(summary["identity_effect"], 0.0)
        self.assertAlmostEqual(summary["phase2_jain"], 1.0)
        self.assertTrue(summary["valid"])

    def test_incumbent_advantage_cancels_flow_identity_effect(self):
        rows, boundaries = analyze._synthetic_rows(phase2_a=0.7, phase4_a=0.3)
        summary = analyze.compute_summary_from_rows(rows, boundaries)
        self.assertAlmostEqual(summary["phase2_incumbent_share"], 0.7)
        self.assertAlmostEqual(summary["phase4_incumbent_share"], 0.7)
        self.assertAlmostEqual(summary["incumbency_effect"], 0.2)
        self.assertAlmostEqual(summary["identity_effect"], 0.0)

    def test_a_identity_advantage_cancels_incumbency_effect(self):
        rows, boundaries = analyze._synthetic_rows(phase2_a=0.7, phase4_a=0.7)
        summary = analyze.compute_summary_from_rows(rows, boundaries)
        self.assertAlmostEqual(summary["identity_effect"], 0.2)
        self.assertAlmostEqual(summary["incumbency_effect"], 0.0)

    def test_baseline_asymmetry_is_flagged_before_fairness_interpretation(self):
        rows, boundaries = analyze._synthetic_rows()
        for row in rows:
            if row["phase"] == "B only" and row["flow"] == "B":
                row["throughput_mbps"] = 12.0
        summary = analyze.compute_summary_from_rows(rows, boundaries)
        self.assertIn("BASELINE_ASYMMETRY", summary["issues"])
        self.assertFalse(summary["valid"])

    def test_convergence_requires_consecutive_bins(self):
        times = [0, 0.25, 0.5, 0.75, 1.0, 1.25]
        shares = [0.8, 0.5, 0.7, 0.54, 0.52, 0.51]
        self.assertEqual(convergence_time(times, shares, 0, 0.45, 0.55, 3), 0.75)

    def test_no_convergence_returns_none(self):
        self.assertIsNone(convergence_time([0, 1, 2, 3], [0.7, 0.7, 0.65, 0.62], 0))

    def test_phase2_convergence_cannot_be_satisfied_by_later_phase4(self):
        rows, boundaries = analyze._synthetic_rows(phase2_a=0.7, phase4_a=0.5)
        summary = analyze.compute_summary_from_rows(rows, boundaries)
        self.assertIsNone(summary["phase2_convergence_s"])
        self.assertIsNotNone(summary["phase4_convergence_s"])

    def test_nonfinite_summary_values_are_json_sanitized(self):
        safe = analyze._json_safe({"missing": math.nan, "nested": [1.0, math.inf]})
        self.assertEqual(safe, {"missing": None, "nested": [1.0, None]})
        json.dumps(safe, allow_nan=False)

    def test_jain_fairness_matches_equal_and_unequal_allocations(self):
        self.assertEqual(jain_fairness((10, 10)), 1.0)
        self.assertAlmostEqual(jain_fairness((14, 6)), 400 / (2 * (196 + 36)))

    def test_actual_phase_boundaries_reject_missing_or_reordered_events(self):
        events = [
            {"instance": "A1", "event": "start", "time_s": 0},
            {"instance": "B1", "event": "start", "time_s": 15},
            {"instance": "A1", "event": "end", "time_s": 30},
            {"instance": "A2", "event": "start", "time_s": 45},
            {"instance": "B1", "event": "end", "time_s": 60},
            {"instance": "A2", "event": "end", "time_s": 75},
        ]
        self.assertEqual(analyze.actual_phase_boundaries(events), [0, 15, 30, 45, 60, 75])
        broken = [dict(row) for row in events]
        broken[3]["time_s"] = 20
        with self.assertRaises(analyze.AnalysisError):
            analyze.actual_phase_boundaries(broken)

    def test_packet_bins_and_transport_state_create_canonical_long_timeline(self):
        metadata = {"flows": [{"instance": f.instance} for f in FLOW_INSTANCES]}
        events = [
            {"instance": "A1", "event": "start", "time_s": 0},
            {"instance": "B1", "event": "start", "time_s": 1},
            {"instance": "A1", "event": "end", "time_s": 2},
            {"instance": "A2", "event": "start", "time_s": 3},
            {"instance": "B1", "event": "end", "time_s": 4},
            {"instance": "A2", "event": "end", "time_s": 5},
        ]
        packets = [
            analyze.PacketSample(1.10, 125000, "A1", 0, False),
            analyze.PacketSample(1.10, 125000, "B1", 0, True),
            analyze.PacketSample(3.10, 175000, "A2", 0, False),
            analyze.PacketSample(3.10, 75000, "B1", 0, False),
        ]
        transports = {
            "A1": [analyze.TransportSample(1.0, 64000, 15, 1, 0)],
            "B1": [analyze.TransportSample(1.0, 64000, 15, 1, 0)],
            "A2": [analyze.TransportSample(3.0, 64000, 15, 1, 0)],
        }
        rows, boundaries = analyze.build_timeline(
            metadata, events, packets, transports, bin_seconds=0.25
        )
        phase2_a = next(row for row in rows
                        if row["flow"] == "A" and math.isclose(row["time_s"], 1.125))
        phase2_b = next(row for row in rows
                        if row["flow"] == "B" and math.isclose(row["time_s"], 1.125))
        self.assertAlmostEqual(phase2_a["throughput_mbps"], 4.0)
        self.assertAlmostEqual(phase2_b["throughput_mbps"], 4.0)
        self.assertAlmostEqual(phase2_a["flow_share"], 0.5)
        self.assertEqual(phase2_b["retransmissions"], 1)
        self.assertEqual(phase2_a["cwnd_bytes"], 64000)
        self.assertEqual(boundaries, [0, 1, 2, 3, 4, 5])


class VisualizationTests(unittest.TestCase):
    def events(self):
        return [
            {"instance": "A1", "event": "start", "time_s": 0},
            {"instance": "B1", "event": "start", "time_s": 15},
            {"instance": "A1", "event": "end", "time_s": 30},
            {"instance": "A2", "event": "start", "time_s": 45},
            {"instance": "B1", "event": "end", "time_s": 60},
            {"instance": "A2", "event": "end", "time_s": 75},
        ]

    def assert_pair(self, stem):
        svg, png = stem.with_suffix(".svg"), stem.with_suffix(".png")
        self.assertGreater(svg.stat().st_size, 1000)
        self.assertGreater(png.stat().st_size, 1000)
        self.assertEqual(png.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_standard_run_and_aggregate_figures_render_svg_and_png(self):
        rows, boundaries = analyze._synthetic_rows(phase2_a=0.6, phase4_a=0.4)
        summary = analyze.compute_summary_from_rows(rows, boundaries)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            visualization.plot_fairness_timeline(
                rows, self.events(), boundaries, analyze.PHASE_LABELS, 20,
                root / "timeline", run_label="rep_001"
            )
            visualization.plot_convergence_comparison(rows, boundaries, root / "convergence")
            visualization.plot_repetition_phase_summary([summary, summary], root / "phase_summary")
            visualization.plot_solo_check([summary, summary], root / "solo_check", 20)
            for name in ("timeline", "convergence", "phase_summary", "solo_check"):
                self.assert_pair(root / name)


if __name__ == "__main__":
    unittest.main()
