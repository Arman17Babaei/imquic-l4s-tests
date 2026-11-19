"""CPU-only contracts for the ideal payload link and its native wire peer."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ideal_link', ROOT / 'tools/l4s/ideal_link.py')


def fixture(root, sizes=(1000, 2000), tracks=2):
    tiles = []
    for i in range(tracks):
        payloads = [bytes([i + j + 1]) * n for j, n in enumerate(sizes)]
        content = b''.join(payloads)
        (root / f'{i}.bin').write_bytes(content)
        offset = 0
        objects = []
        for j, payload in enumerate(payloads):
            objects.append(dict(id=j, offset=offset, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(), publisher_priority=j))
            offset += len(payload)
        tiles.append(dict(id=str(i), refinements=[dict(index=0, file=f'{i}.bin', track=f'scene/{i}/0', bytes=offset, objects=objects, sha256=hashlib.sha256(content).hexdigest())]))
    manifest = root / 'manifest.json'
    manifest.write_text(json.dumps(dict(scene=dict(id='scene'), tiles=tiles)))
    return manifest


def command(tracks=2):
    return dict(type='open', scene='scene', epoch=0, fetches=[dict(tile_id=str(i), refinement=0, track=f'scene/{i}/0', priority=10) for i in range(tracks)])


class IdealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'tools/l4s'))
        cls.module = importlib.util.module_from_spec(SPEC)
        sys.modules[SPEC.name] = cls.module
        SPEC.loader.exec_module(cls.module)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = fixture(self.root)

    def model(self, delay=0, budget=2**24, sizes=(1000, 2000)):
        class Peer:
            def __init__(self):
                self.items = iter((dict(tile_id='0', refinement=0, object_id=i,
                    payload_bytes=size), bytes(size)) for i, size in enumerate(sizes))
            def next_object(self):
                return next(self.items, None)
        return self.module.OfflineDelivery(Peer(), 8, delay, budget)

    def test_partial_objects_and_exact_delay_boundaries(self):
        from fractions import Fraction
        model = self.model(delay=10)
        self.assertEqual([e for e in model.advance(Fraction(1, 2000)) if e['event']=='arrival'], [])
        events = model.advance(Fraction(11, 1000))
        self.assertEqual([(e['object_id'], e['arrival']) for e in events if e['event']=='arrival'], [(0, .011)])
        events = model.advance(Fraction(13, 1000))
        self.assertEqual([(e['object_id'], e['arrival']) for e in events if e['event']=='arrival'], [(1, .013)])

    def test_completed_arrivals_retire_before_next_object_budget(self):
        from fractions import Fraction
        model = self.model(budget=2000)
        events = model.advance(Fraction(1))
        self.assertEqual(sum(e['payload_bytes'] for e in events if e['event']=='arrival'), 3000)
        self.assertEqual(model.peak_queue_bytes, 2000)

    def test_propagation_budget_fails_without_slowing_link(self):
        from fractions import Fraction
        with self.assertRaisesRegex(ValueError, 'queue budget'):
            self.model(delay=10, budget=2500).advance(Fraction(1))

    def test_invalid_rate_delay_and_decreasing_time(self):
        from fractions import Fraction
        for rate, delay in ((0, 1), (-1, 1), (float('inf'), 1), (8, -1), (8, float('nan'))):
            with self.subTest(rate=rate, delay=delay), self.assertRaises(ValueError):
                self.module.OfflineDelivery(None, rate, delay)
        model = self.model()
        model.advance(Fraction(1))
        with self.assertRaises(ValueError): model.advance(Fraction(0))

    def test_reject_corrupt_missing_unsafe_and_duplicate_objects(self):
        original = json.loads(self.manifest.read_text())
        for mutation in ('corrupt', 'missing', 'unsafe', 'duplicate', 'negative'):
            data = json.loads(json.dumps(original))
            layer = data['tiles'][0]['refinements'][0]
            if mutation == 'corrupt': layer['objects'][0]['sha256'] = '0' * 64
            if mutation == 'missing': layer['file'] = 'missing'
            if mutation == 'unsafe': layer['file'] = '../outside'
            if mutation == 'duplicate': layer['objects'][1]['id'] = 0
            if mutation == 'negative': layer['objects'][0]['bytes'] = -1
            self.manifest.write_text(json.dumps(data))
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)):
                self.module.ManifestIndex(self.manifest)
