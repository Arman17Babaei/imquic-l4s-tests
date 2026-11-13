"""Audit actual browser receipt, separately from publisher pipe dispatch."""
import math


def validate_delivery(publisher: dict, events: list[dict], dispatches: list[dict] | None = None, horizon_unix_ms: float | None = None, *, start_unix_ms: float | None = None) -> dict:
    """Audit identities across the run; optionally grade timing in [start, horizon).

    Receipt samples use raw receipt time, dispatch samples use ideal due time.
    Objects due inside the window must also have a receipt inside that window.
    Without a start, retain the original full-run timing interpretation.
    """
    def finite(value):
        return isinstance(value, (float, int)) and math.isfinite(value)

    def in_window(value):
        return finite(value) and value >= start_unix_ms and (horizon_unix_ms is None or value < horizon_unix_ms)

    result = {**publisher, 'publisher_status': publisher.get('status', 'FAIL'),
              'failures': list(publisher.get('failures', []))}
    objects = [event for event in events if event.get('type') == 'object']
    lateness = []
    missing = early = window_early = 0
    for event in objects:
        due, received = event.get('ideal_arrival_unix_ms'), event.get('receivedUnixMs')
        if not all(isinstance(x, (float, int)) and math.isfinite(x) for x in (due, received)):
            missing += 1
            continue
        delta = received - due
        # Both clocks are on this host; preserve a small wall-clock mapping tolerance.
        early += delta < -2
        if start_unix_ms is None or in_window(received):
            window_early += delta < -2
            lateness.append(max(0, delta))
    accounting_valid = True
    if dispatches is not None:
        def identity(event):
            return tuple(event.get(key) for key in ('request_id', 'tile_id', 'refinement', 'object_id'))
        before = [event for event in dispatches if horizon_unix_ms is None or (finite(event.get('ideal_arrival_unix_ms')) and event['ideal_arrival_unix_ms'] < horizon_unix_ms)]
        expected = [identity(event) for event in before]
        received = [identity(event) for event in objects]
        missing_objects = set(expected) - set(received)
        unexpected_objects = set(received) - {identity(event) for event in dispatches}
        duplicate_objects = len(received) - len(set(received))
        dispatch_count_valid = len(dispatches) == publisher.get('delivered_objects')
        accounting_valid = not missing_objects and not unexpected_objects and not duplicate_objects and dispatch_count_valid and all(None not in key for key in expected + received)
        result.update(receiver_missing_objects=len(missing_objects), receiver_unexpected_objects=len(unexpected_objects), receiver_duplicate_objects=duplicate_objects,
                      expected_pre_horizon_objects=len(expected), post_horizon_dispatches=len(dispatches)-len(before),
                      dispatch_count_valid=dispatch_count_valid, horizon_unix_ms=horizon_unix_ms)
        if not accounting_valid:
            result['failures'].append('dispatched object identities/counts do not reconcile with browser receipts')
        if start_unix_ms is not None:
            expected_window = {identity(event) for event in dispatches if in_window(event.get('ideal_arrival_unix_ms'))}
            received_window = {identity(event) for event in objects if in_window(event.get('receivedUnixMs'))}
            window_missing = len(expected_window - received_window)
            result.update(expected_window_objects=len(expected_window), receiver_window_missing_objects=window_missing)
            accounting_valid = accounting_valid and not window_missing
            if window_missing:
                result['failures'].append('objects due inside measurement window lack receipts inside that window')
    ordered = sorted(lateness)
    p95 = ordered[max(0, math.ceil(len(ordered) * .95) - 1)] if ordered else None
    maximum = max(ordered, default=None)
    valid = accounting_valid and bool(ordered) and not missing and not early and p95 <= 5 and maximum <= 20
    result.update(receiver_status='PASS' if valid else 'FAIL', receiver_samples=len(lateness),
                  receiver_missing_timestamps=missing, receiver_early_arrivals=window_early,
                  receiver_lateness_p95_ms=p95, receiver_lateness_max_ms=maximum,
                  receipt_measurement='raw WebSocket callback before asynchronous object validation; local host wall clocks, early tolerance2ms')
    if not valid:
        result['failures'].append('browser receipt violated ideal timing or lacked timestamp evidence (p95<=5ms,max<=20ms)')
    publisher_valid = result['publisher_status'] == 'PASS'
    if start_unix_ms is not None:
        timing_failure = 'dispatch lateness exceeded p95 <= 5 ms / max <= 20 ms'
        raw_failures = list(publisher.get('failures', []))
        timing_metadata_valid = dispatches is not None and len(dispatches) == publisher.get('delivered_objects') and all(
            finite(event.get('ideal_arrival_unix_ms')) and finite(event.get('lateness_ms')) for event in dispatches)
        window_lateness = sorted(max(0, event['lateness_ms']) for event in (dispatches or [])
                                 if in_window(event.get('ideal_arrival_unix_ms')) and finite(event.get('lateness_ms')))
        dispatch_p95 = window_lateness[max(0, math.ceil(len(window_lateness) * .95) - 1)] if window_lateness else None
        dispatch_max = max(window_lateness, default=None)
        window_timing_valid = bool(window_lateness) and timing_metadata_valid and dispatch_p95 <= 5 and dispatch_max <= 20
        # A legacy FAIL can be reconsidered only with explicit structural evidence.
        # Unknown failures or missing invariant counters keep the raw failure.
        regradeable = raw_failures == [timing_failure] and publisher.get('early_deliveries') == 0 and publisher.get('budget_violations') == 0
        publisher_valid = ((publisher_valid and not raw_failures) or regradeable) and window_timing_valid
        publisher_valid = publisher_valid and publisher.get('early_deliveries') == 0 and publisher.get('budget_violations') == 0
        publisher_valid = publisher_valid and not any(event.get('lateness_ms', 0) < -1e-6 for event in (dispatches or []) if finite(event.get('lateness_ms')))
        result.update(start_unix_ms=start_unix_ms, horizon_unix_ms=horizon_unix_ms,
                      publisher_full_run_failures=raw_failures,
                      receiver_full_run_early_arrivals=early,
                      publisher_window_status='PASS' if publisher_valid else 'FAIL',
                      window_dispatch_samples=len(window_lateness), window_dispatch_lateness_p95_ms=dispatch_p95,
                      window_dispatch_lateness_max_ms=dispatch_max, window_dispatch_timestamps_valid=timing_metadata_valid,
                      timing_window='[start_unix_ms, horizon_unix_ms); receipts by receivedUnixMs, dispatches by ideal_arrival_unix_ms')
        if publisher_valid and regradeable:
            result['failures'] = [failure for failure in result['failures'] if failure != timing_failure]
        elif not publisher_valid:
            result['failures'].append('publisher measurement window lacks valid timing or full-run structural evidence')
    result['status'] = 'PASS' if valid and publisher_valid else 'FAIL'
    return result
