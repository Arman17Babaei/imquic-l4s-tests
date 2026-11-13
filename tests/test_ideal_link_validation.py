import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/l4s'))
from ideal_link_validation import validate_delivery

class DeliveryTimingTest(unittest.TestCase):
    def test_raw_receipt_not_async_validation_time_sets_delivery_lateness(self):
        summary=dict(status='PASS',failures=[])
        events=[dict(type='object',ideal_arrival_unix_ms=1000,receivedUnixMs=1001,timestampMs=9000)]
        result=validate_delivery(summary,events)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['receiver_lateness_max_ms'],1)
        self.assertEqual(summary,dict(status='PASS',failures=[]))

    def test_buffered_tail_fails_even_when_publisher_pipe_write_passes(self):
        result=validate_delivery(dict(status='PASS',failures=[]),[dict(type='object',ideal_arrival_unix_ms=1000,receivedUnixMs=1025)])
        self.assertEqual(result['status'],'FAIL')
        self.assertEqual(result['publisher_status'],'PASS')

    def test_missing_or_early_receipt_metadata_cannot_pass(self):
        for events in ([],[dict(type='object')],[dict(type='object',ideal_arrival_unix_ms=1000,receivedUnixMs=990)]):
            with self.subTest(events=events):
                self.assertEqual(validate_delivery(dict(status='PASS',failures=[]),events)['status'],'FAIL')

    def test_missing_dispatched_tail_fails(self):
        event=dict(type='object',request_id=1,tile_id=2,refinement=0,object_id=0,ideal_arrival_unix_ms=1000,receivedUnixMs=1001)
        dispatches=[dict(event,event='dispatch'),dict(event,event='dispatch',object_id=1)]
        result=validate_delivery(dict(status='PASS',delivered_objects=2),[event],dispatches,1100)
        self.assertEqual(result['status'],'FAIL')
        self.assertEqual(result['receiver_missing_objects'],1)

    def test_post_horizon_dispatch_is_explicitly_excluded(self):
        event=dict(type='object',request_id=1,tile_id=2,refinement=0,object_id=0,ideal_arrival_unix_ms=1000,receivedUnixMs=1001)
        dispatches=[dict(event,event='dispatch'),dict(event,event='dispatch',object_id=1,ideal_arrival_unix_ms=1200)]
        result=validate_delivery(dict(status='PASS',delivered_objects=2),[event],dispatches,1100)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['post_horizon_dispatches'],1)

    def test_unknown_receipt_identity_fails(self):
        event=dict(type='object',request_id=1,tile_id=2,refinement=0,object_id=0,ideal_arrival_unix_ms=1000,receivedUnixMs=1001)
        result=validate_delivery(dict(status='PASS',delivered_objects=1),[event,dict(event,object_id=99)],[dict(event,event='dispatch')],1100)
        self.assertEqual(result['status'],'FAIL')

    def window_fixture(self):
        events = [dict(type='object', request_id=1, tile_id=2, refinement=0,
                       object_id=index, ideal_arrival_unix_ms=due, receivedUnixMs=received)
                  for index, (due, received) in enumerate(((900, 930), (1000, 1001), (1200, 1230)))]
        dispatches = [dict(event, lateness_ms=event['receivedUnixMs'] - event['ideal_arrival_unix_ms']) for event in events]
        publisher = dict(status='FAIL', failures=['dispatch lateness exceeded p95 <= 5 ms / max <= 20 ms'],
                         delivered_objects=3, early_deliveries=0, budget_violations=0)
        return publisher, events, dispatches

    def test_window_excludes_startup_and_post_horizon_timing_but_keeps_audit(self):
        publisher, events, dispatches = self.window_fixture()
        result = validate_delivery(publisher, events, dispatches, 1100, start_unix_ms=1000)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['publisher_status'], 'FAIL')
        self.assertEqual(result['publisher_window_status'], 'PASS')
        self.assertEqual(result['publisher_full_run_failures'], publisher['failures'])
        self.assertEqual(result['receiver_samples'], 1)
        self.assertEqual(result['receiver_lateness_max_ms'], 1)
        self.assertEqual(result['window_dispatch_samples'], 1)
        self.assertEqual(result['receiver_missing_objects'], 0)
        self.assertEqual(result['expected_pre_horizon_objects'], 2)
        self.assertEqual(result['post_horizon_dispatches'], 1)

    def test_late_receipt_after_horizon_is_missing_from_delivery_window(self):
        publisher, events, dispatches = self.window_fixture()
        events[1]['receivedUnixMs'] = 1100
        result = validate_delivery(publisher, events, dispatches, 1100, start_unix_ms=1000)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['receiver_samples'], 0)
        self.assertEqual(result['receiver_missing_objects'], 0)
        self.assertEqual(result['receiver_window_missing_objects'], 1)

    def test_window_cannot_hide_structural_errors_or_missing_metadata(self):
        for mutation in ('budget', 'early', 'unknown_failure', 'missing_counter', 'missing_dispatch_time', 'missing_receipt_time', 'duplicate', 'unknown', 'missing_startup'):
            with self.subTest(mutation=mutation):
                publisher, events, dispatches = self.window_fixture()
                if mutation == 'budget': publisher['budget_violations'] = 1
                elif mutation == 'early': publisher['early_deliveries'] = 1
                elif mutation == 'unknown_failure': publisher['failures'].append('other failure')
                elif mutation == 'missing_counter': del publisher['budget_violations']
                elif mutation == 'missing_dispatch_time': del dispatches[0]['ideal_arrival_unix_ms']
                elif mutation == 'missing_receipt_time': del events[0]['receivedUnixMs']
                elif mutation == 'duplicate': events.append(dict(events[0]))
                elif mutation == 'unknown': events.append(dict(events[0], object_id=99))
                elif mutation == 'missing_startup': events.pop(0)
                result = validate_delivery(publisher, events, dispatches, 1100, start_unix_ms=1000)
                self.assertEqual(result['status'], 'FAIL')

    def test_window_preserves_full_run_early_arrival_evidence(self):
        publisher, events, dispatches = self.window_fixture()
        events[0]['receivedUnixMs'] = 890
        result = validate_delivery(publisher, events, dispatches, 1100, start_unix_ms=1000)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['receiver_early_arrivals'], 0)
        self.assertEqual(result['receiver_full_run_early_arrivals'], 1)

    def test_window_pass_requires_explicit_publisher_invariants(self):
        publisher, events, dispatches = self.window_fixture()
        publisher.update(status='PASS', failures=[])
        del publisher['budget_violations']
        result = validate_delivery(publisher, events, dispatches, 1100, start_unix_ms=1000)
        self.assertEqual(result['status'], 'FAIL')

    def test_legacy_full_run_is_not_automatically_regraded(self):
        publisher, events, dispatches = self.window_fixture()
        result = validate_delivery(publisher, events, dispatches, 1100)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['receiver_samples'], 3)
        self.assertEqual(result['receiver_lateness_max_ms'], 30)
        self.assertNotIn('publisher_window_status', result)

    def test_window_requires_complete_dispatch_timing_metadata(self):
        for key in ('ideal_arrival_unix_ms', 'lateness_ms'):
            publisher, events, dispatches = self.window_fixture()
            del dispatches[2][key]
            result = validate_delivery(publisher, events, dispatches, 1100, start_unix_ms=1000)
            self.assertEqual(result['status'], 'FAIL')
            self.assertFalse(result['window_dispatch_timestamps_valid'])
