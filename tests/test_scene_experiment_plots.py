"""Retained-evidence plot analysis does not require a VM or browser."""
import importlib.util
import math
from pathlib import Path
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'tools/l4s'
sys.path.insert(0, str(TOOLS))


class ScenePlotsTest(unittest.TestCase):
    def module(self):
        self.assertTrue((TOOLS / 'plot_scene_experiments.py').is_file(), 'plot module must exist')
        import plot_scene_experiments
        return plot_scene_experiments

    def test_queue_labels_follow_recorded_state_not_controller_or_new_defaults(self):
        module = self.module()
        old = "[provider_egress] device=s1-eth2\nqdisc dualpi2 10: target 15ms tupdate 16ms step_thresh 5ms\n[downstream_egress] device=s2-eth2\nqdisc pfifo 20: parent 1:1 limit 128p\n"
        new = old.replace('qdisc pfifo 20: parent 1:1 limit 128p',
                          'qdisc dualpi2 10: target 15ms tupdate 16ms step_thresh 1ms')
        self.assertEqual(module.recorded_queue(old,'downstream_egress'),'FIFO 128 packets')
        self.assertIn('step 5ms', module.recorded_queue(old,'provider_egress'))
        self.assertIn('step 1ms', module.recorded_queue(new,'downstream_egress'))
        self.assertEqual(module.recorded_queue('', 'downstream_egress'), 'unrecorded')
        cases=[{'summary': {'controller':'reno','downstream_mbps':100,'downstream_queue':module.recorded_queue(old,'downstream_egress')}},
               {'summary': {'controller':'prague','downstream_mbps':100,'downstream_queue':module.recorded_queue(new,'downstream_egress')}}]
        label=module.queue_labels(cases,'downstream_queue')
        self.assertIn('reno/100Mbps = FIFO 128 packets',label)
        self.assertIn('prague/100Mbps = DualPI2',label)
        self.assertIn('step 1ms',label)

    def test_provider_rate_and_delay_come_from_saved_state(self):
        module=self.module()
        state='[provider_egress] device=s1-eth2\nclass htb 1:1 root rate 20Mbit ceil 20Mbit burst 32Kb/1\n[client_egress] device=client-eth0\nqdisc noqueue 0: root\n'
        self.assertEqual(module.recorded_htb_rate(state,'provider_egress'),'20Mbit')
        self.assertEqual(module.recorded_delay(state,'client_egress'),'none added')
        self.assertEqual(module.recorded_delay(state.replace('qdisc noqueue 0: root','qdisc netem 1: root delay 10ms'),'client_egress'),'10ms')
        self.assertEqual(module.recorded_delay(state,'server_egress'),'unrecorded')

    def test_payload_bins_include_zero_intervals_and_exclude_horizon(self):
        module = self.module()
        telemetry = {'trace': {'startedAtMs': 1000, 'nominalDurationMs': 400}, 'events': [
            {'type': 'object', 'timestampMs': 900, 'payload_bytes': 20},
            {'type': 'object', 'timestampMs': 1000, 'payload_bytes': 100},
            {'type': 'object', 'timestampMs': 1099, 'payload_bytes': 200},
            {'type': 'object', 'timestampMs': 1250, 'payload_bytes': 50},
            {'type': 'object', 'timestampMs': 1400, 'payload_bytes': 1000},
            {'type': 'object', 'timestampMs': 1350, 'payload_bytes': 999, 'ignored': True},
        ]}
        rows = module.payload_bins(telemetry)
        self.assertEqual([row['payload_bytes'] for row in rows], [300, 0, 50, 0])
        self.assertAlmostEqual(rows[0]['payload_mbps'], .024)
        self.assertEqual(module.zero_intervals(rows), [
            {'start_seconds': .1, 'end_seconds': .2, 'duration_seconds': .1},
            {'start_seconds': .3, 'end_seconds': .4, 'duration_seconds': .1}])

    def test_guest_monotonic_alignment_uses_recorded_wall_mapping(self):
        module = self.module()
        samples = [{'unix_ns': 10000000000, 'monotonic_ns': 2000000000},
                   {'unix_ns': 10100000000, 'monotonic_ns': 2100000000}]
        self.assertAlmostEqual(module.guest_wall_offset_ms(samples), 8000)
        self.assertAlmostEqual(module.relay_seconds({'monotonic_us': '2300000'}, 8000, 10000), .3)

    def test_rate_comparisons_pair_only_identical_scene_and_controller(self):
        module = self.module()
        def case(scene, controller, rate):
            return {'summary': {'scene': scene, 'controller': controller, 'downstream_mbps': rate}}
        bicycle20 = case('bicycle', 'prague', 20)
        bicycle100 = case('bicycle', 'prague', 100)
        unpaired = [case('bicycle', 'reno', 100), case('train', 'prague', 100),
                    case('playroom', 'prague', 20)]
        self.assertEqual(module.rate_comparison_groups([bicycle100, *unpaired, bicycle20]),
                         [('bicycle', 'prague', [bicycle20, bicycle100])])
        self.assertEqual(module.rate_comparison_groups(unpaired), [])

    def test_native_stream_rates_use_actual_intervals_and_separate_connections(self):
        module = self.module()
        def row(connection, time_us, sent, received):
            return {'connection': connection, 'monotonic_us': time_us,
                    'data_sent_bytes': sent, 'data_received_bytes': received}
        rows = module.native_stream_rates([
            row('a', 1000000, 1000, 500), row('b', 1100000, 9000000, 0),
            row('a', 1250000, 2000, 750), row('a', 1750000, 2000, 750),
            row('b', 1600000, 9001000, 500), row('a', 1800000, 100, 20),
            row('a', 1700000, 200, 30)])
        self.assertTrue(math.isnan(rows[0]['native_stream_sent_mbps']))
        self.assertTrue(math.isnan(rows[1]['native_stream_sent_mbps']))
        self.assertAlmostEqual(rows[2]['native_stream_interval_seconds'], .25)
        self.assertAlmostEqual(rows[2]['native_stream_sent_mbps'], .032)
        self.assertAlmostEqual(rows[2]['native_stream_received_mbps'], .008)
        self.assertEqual(rows[3]['native_stream_sent_mbps'], 0)
        self.assertAlmostEqual(rows[4]['native_stream_sent_mbps'], .016)
        self.assertTrue(math.isnan(rows[5]['native_stream_sent_mbps']))
        self.assertTrue(math.isnan(rows[6]['native_stream_sent_mbps']))


if __name__ == '__main__': unittest.main()
