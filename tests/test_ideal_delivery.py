"""Object-accounting checks, independent of browser/publisher wall-clock speed."""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from ideal_delivery import validate_delivery


def obj(object_id=0, due=1000):
    return dict(request_id=1, tile_id='tile', refinement=0, object_id=object_id,
                ideal_arrival_unix_ms=due)


class IdealDeliveryTest(unittest.TestCase):
    def test_offline_delivery_uses_simulated_arrivals_and_checks_payload_bytes(self):
        dispatch = dict(request_id=1, tile_id='tile', refinement=0, object_id=0,
                        ideal_arrival_ms=25, payload_bytes=100)
        summary = dict(status='PASS', delivery_model='offline-imquic-v1', delivered_objects=1,
                       delivered_payload_bytes=100)
        result = validate_delivery(summary, [dict(dispatch, type='object')], [dispatch])
        self.assertEqual(result['status'], 'PASS')
        result = validate_delivery(summary, [dict(dispatch, type='object', payload_bytes=99)], [dispatch])
        self.assertEqual(result['status'], 'FAIL')
    def test_delayed_intact_receipt_passes_without_timing_fields(self):
        dispatch = obj()
        result = validate_delivery({'status': 'PASS', 'delivered_objects': 1},
            [{'type': 'object', **dispatch, 'receivedUnixMs': 9000}], [dispatch], horizon_unix_ms=1100)
        self.assertEqual(result['status'], 'PASS')
        self.assertFalse(any('lateness' in key or 'timing' in key for key in result))
        self.assertNotIn('receiver_window_missing_objects', result)

    def test_missing_unexpected_duplicate_and_invalid_identity_fail(self):
        dispatches = [obj()]
        for objects in ([], [obj(1)], [obj(), obj()], [{**obj(), 'tile_id': None}]):
            with self.subTest(objects=objects):
                result = validate_delivery({'status': 'PASS', 'delivered_objects': 1},
                    [{'type': 'object', **event} for event in objects], dispatches)
                self.assertEqual(result['status'], 'FAIL')

    def test_model_horizon_censors_future_objects_without_receipt_deadline(self):
        dispatches = [obj(0, 1000), obj(1, 1200)]
        result = validate_delivery({'status': 'PASS', 'delivered_objects': 2},
            [{'type': 'object', **obj(0), 'receivedUnixMs': 5000}], dispatches, horizon_unix_ms=1100)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['expected_objects'], 1)

    def test_invalid_model_timestamp_count_or_publisher_failure_fails(self):
        for publisher, dispatches in (({'status': 'PASS', 'delivered_objects': 2}, [obj()]),
                                      ({'status': 'FAIL', 'delivered_objects': 1}, [obj()]),
                                      ({'status': 'PASS', 'delivered_objects': 1}, [obj(due=math.nan)])):
            with self.subTest(publisher=publisher, dispatches=dispatches):
                result = validate_delivery(publisher, [{'type': 'object', **obj()}], dispatches)
                self.assertEqual(result['status'], 'FAIL')

    def test_empty_delivery_before_first_object_is_valid(self):
        result = validate_delivery({'status': 'PASS', 'delivered_objects': 0}, [], [], horizon_unix_ms=100)
        self.assertEqual(result['status'], 'PASS')
