"""Reconcile modeled objects with browser receipts, without wall-clock grading."""
import math


def validate_delivery(publisher: dict, events: list[dict], dispatches: list[dict],
                      horizon_unix_ms: float | None = None) -> dict:
    def identity(event):
        tile = event.get('tile_id')
        numbers = tuple(event.get(key) for key in ('request_id', 'refinement', 'object_id'))
        if not isinstance(tile, str) or not tile or any(type(value) is not int or value < 0 for value in numbers):
            return None
        return (tile, *numbers)

    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    objects = [event for event in events if event.get('type') == 'object']
    dispatched = [identity(event) for event in dispatches]
    received = [identity(event) for event in objects]
    offline = publisher.get('delivery_model') == 'offline-imquic-v1'
    timestamp_key = 'ideal_arrival_ms' if offline else 'ideal_arrival_unix_ms'
    metadata_valid = (horizon_unix_ms is None or finite(horizon_unix_ms)) and all(
        finite(event.get(timestamp_key)) for event in dispatches)
    expected = {identity(event) for event in dispatches if horizon_unix_ms is None or
                (finite(event.get(timestamp_key)) and event[timestamp_key] < horizon_unix_ms)}
    missing = expected - set(received)
    unexpected = set(received) - set(dispatched)
    duplicates = len(received) - len(set(received))
    counts_valid = len(dispatches) == publisher.get('delivered_objects') and len(dispatched) == len(set(dispatched))
    if offline:
        sent = {identity(event): event for event in dispatches}
        counts_valid = counts_valid and publisher.get('delivered_payload_bytes') == sum(
            event.get('payload_bytes', -1) for event in dispatches)
        metadata_valid = metadata_valid and all(identity(event) in sent and
            event.get('payload_bytes') == sent[identity(event)].get('payload_bytes') and
            event.get(timestamp_key) == sent[identity(event)].get(timestamp_key) for event in objects)
    valid = (publisher.get('status') == 'PASS' and not publisher.get('failures') and metadata_valid and counts_valid and
             None not in dispatched + received and not missing and not unexpected and not duplicates)
    failures = list(publisher.get('failures', []))
    if not valid:
        failures.append('publisher/object identities, modeled metadata or receipt counts do not reconcile')
    return {**publisher, 'status': 'PASS' if valid else 'FAIL', 'failures': failures,
            'expected_objects': len(expected), 'missing_objects': len(missing),
            'unexpected_objects': len(unexpected), 'duplicate_objects': duplicates,
            'dispatch_count_valid': counts_valid, 'model_metadata_valid': metadata_valid}
