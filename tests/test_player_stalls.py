"""Synthetic acceptance tests for saved-run stall classification; no live services."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1] / 'tools/l4s/analyze_player_stalls.py'


class PlayerStallsTest(unittest.TestCase):
    def analyze(self, objects, *, source=100, received=20, native=None, duration=4000, trace_fields=None):
        spec = importlib.util.spec_from_file_location('player_stalls_test', TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        events = [{'type': 'object', 'timestampMs': 1000 + at, 'monotonic_us': (11000 + at) * 1000,
                   'payload_bytes': 100, 'object_id': index} for index, at in enumerate(objects)]
        telemetry = {'sourceGaussians': source, 'receivedGaussians': received, 'appliedGaussians': received,
                     'events': events, 'trace': {'startedAtMs': 1000, 'startedUnixMs': 101000,
                                               'nominalDurationMs': duration}}
        telemetry['trace'].update(trace_fields or {})
        rows = native if native is not None else [
            {'monotonic_us': int((11000 + i * 100) * 1000), 'data_sent_bytes': i * 100,
             'data_received_bytes': 0, 'sendable_bytes': 10000, 'queued_stream_bytes': 10000,
             'connection_credit_bytes': 100000, 'flow_blocked_streams': 0,
             'cwnd_bytes': 10000, 'bytes_in_flight': 2000, 'packets_lost': 0,
             'pacing_Bps': 1000000} for i in range(duration // 100 + 1)]
        samples = [{'monotonic_ns': 11000 * 1000000, 'unix_ns': 101000 * 1000000}]
        return module.analyze(telemetry, relay_rows=rows, network_samples=samples)

    def test_bursty_transfer_with_subsecond_gaps_passes(self):
        result = self.analyze(list(range(0, 4001, 400)))
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['candidates'], [])

    def test_completed_transfer_idle_is_expected(self):
        result = self.analyze([0, 200, 400], received=100)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['expected_idle_after_complete_ms'], 3600)

    def test_posthorizon_objects_cannot_fill_a_gap(self):
        result = self.analyze([0, 200, 4500, 4700])
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(result['excluded_posthorizon_objects'], 2)
        self.assertTrue(any(c['kind'] == 'application_object_gap' for c in result['candidates']))

    def test_posthorizon_drain_cannot_prove_horizon_completion(self):
        result = self.analyze([0, 200, 4500, 4700], received=100,
                              trace_fields={'receivedGaussiansAtHorizon': 20, 'gaussiansAtHorizon': 20})
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(result['expected_idle_after_complete_ms'], 0)
        self.assertEqual(result['observation']['pending_end_browser_ms'], 5000)

    def test_old_run_with_posthorizon_drain_remains_pending(self):
        result = self.analyze([0, 200, 4500, 4700], received=100)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(result['expected_idle_after_complete_ms'], 0)

    def test_pretrace_objects_are_excluded_from_observation_gaps(self):
        result = self.analyze([-2000, -100, *range(0, 4001, 400)])
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['excluded_pretrace_objects'], 2)

    def test_native_sender_stall_is_detected_despite_app_buffering(self):
        rows = [{'monotonic_us': (11000 + i * 100) * 1000,
                 'data_sent_bytes': 100 if 5 <= i <= 25 else (i * 100 if i < 5 else (i - 24) * 100),
                 'sendable_bytes': 10000, 'queued_stream_bytes': 10000,
                 'connection_credit_bytes': 100000, 'flow_blocked_streams': 0,
                 'cwnd_bytes': 10000, 'bytes_in_flight': 1000} for i in range(41)]
        # Keep the cumulative counter monotone across the stalled interval.
        for row in rows:
            at = row['monotonic_us'] / 1000 - 11000
            row['data_sent_bytes'] = int(min(at, 500) + max(0, at - 2500))
        result = self.analyze(list(range(0, 4001, 100)), native=rows)
        self.assertEqual(result['status'], 'REVIEW')
        candidate = next(c for c in result['candidates'] if c['kind'] == 'native_sender_no_progress')
        self.assertEqual(candidate['classification'], 'eligible_data_without_send_progress')
        self.assertGreaterEqual(candidate['duration_ms'], 1900)

    def test_long_app_gap_with_native_progress_still_requires_review(self):
        result = self.analyze([0, 100, 1800, 2200, 2600, 3000, 3400, 3800])
        self.assertEqual(result['status'], 'REVIEW')
        candidate = next(c for c in result['candidates'] if c['kind'] == 'application_object_gap')
        self.assertGreater(candidate['relay']['data_sent_bytes']['delta'], 0)
        self.assertEqual(candidate['classification'], 'application_gap_with_native_byte_progress')

    def test_missing_native_evidence_cannot_certify_health(self):
        result = self.analyze(list(range(0, 4001, 400)), native=[])
        self.assertEqual(result['status'], 'REVIEW')

    def test_native_counter_reset_requires_review(self):
        rows = [{'monotonic_us': (11000 + i * 100) * 1000,
                 'data_sent_bytes': (i if i < 20 else i - 20) * 100,
                 'sendable_bytes': 1000} for i in range(41)]
        result = self.analyze(list(range(0, 4001, 400)), native=rows)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertTrue(any('counter decreased' in note for note in result['limitations']))

    def test_sender_idle_after_full_delivery_is_not_a_stall(self):
        rows = [{'monotonic_us': (11000 + i * 100) * 1000,
                 'data_sent_bytes': min(i, 4) * 100, 'sendable_bytes': 0,
                 'queued_stream_bytes': 0, 'bytes_in_flight': 0} for i in range(41)]
        result = self.analyze([0, 200, 400], received=100, native=rows)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['candidates'], [])

    def test_bad_cli_arguments_have_exit_one(self):
        result = subprocess.run([sys.executable, str(TOOL), '--gap-seconds', 'not-a-number'],
                                capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_bad_cli_input_has_exit_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, str(TOOL), '--run', temporary,
                                     '--output', str(Path(temporary) / 'report.json')], capture_output=True)
        self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main()
