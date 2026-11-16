"""CPU-only contracts for the ideal payload link and its native wire peer."""
import hashlib
import importlib.util
import json
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import time
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
        cls.module = importlib.util.module_from_spec(SPEC)
        sys.modules[SPEC.name] = cls.module
        SPEC.loader.exec_module(cls.module)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = fixture(self.root)

    def scheduler(self, delay=0, budget=2**24):
        return self.module.IdealScheduler(self.module.ManifestIndex(self.manifest), 8, delay, budget)

    def test_shared_bandwidth_exact_bytes_and_stable_order(self):
        s = self.scheduler()
        s.open(command(), 0)
        events = s.advance(1)
        selected = [e for e in events if e['event'] == 'serialization']
        self.assertEqual([(e['tile_id'], e['object_id']) for e in selected], [('0', 0), ('0', 1), ('1', 0), ('1', 1)])
        self.assertEqual([round(e['finish'], 6) for e in selected], [.001, .003, .004, .006])
        self.assertEqual(sum(e['payload_bytes'] for e in selected), 6000)

    def test_precise_scores_break_integer_ties_and_updates_apply_at_boundaries(self):
        s = self.scheduler()
        c = command()
        c['fetches'][0].update(priority=10, priority_score=10.9)
        c['fetches'][1].update(priority=10, priority_score=10.1)
        s.open(c, 0)
        self.assertEqual(s.advance(0)[0]['tile_id'], '1')
        s.update(dict(epoch=1, updates=[dict(request_id=1, priority=10, priority_score=10.05)]), .0005)
        selected = [e for e in s.advance(.004) if e['event']=='serialization']
        self.assertEqual(selected[0]['tile_id'], '0')
        self.assertEqual(selected[0]['subscriber_score'], 10.05)

    def test_invalid_precise_scores_cannot_change_integer_priority_class(self):
        for score in (float('nan'), float('inf'), -1, True, 11.1):
            with self.subTest(score=score):
                c = command()
                c['fetches'][0].update(priority=10, priority_score=score)
                with self.assertRaises(ValueError):
                    self.scheduler().open(c, 0)

    def test_delay_shifts_arrivals_without_reducing_rate(self):
        records = []
        for delay in (10, 50):
            s = self.scheduler(delay)
            s.open(command(), 0)
            records.append([e for e in s.advance(1) if e['event'] == 'arrival'])
        self.assertEqual(len(records[0]), 4)
        for low, high in zip(*records):
            self.assertAlmostEqual(high['arrival'] - low['arrival'], .040)
            self.assertEqual(high['finish'], low['finish'])

    def test_update_only_changes_next_object_boundary(self):
        s = self.scheduler()
        s.open(command(), 0)
        initial = s.advance(0)
        self.assertEqual(initial[0]['tile_id'], '0')
        s.update(dict(epoch=4, updates=[dict(request_id=2, priority=0)]), .0005)
        selected = [e for e in s.advance(.004) if e['event'] == 'serialization']
        self.assertEqual([(e['tile_id'], e['object_id']) for e in selected[:2]], [('1', 0), ('1', 1)])
        self.assertEqual(selected[0]['subscriber_epoch'], 4)
        self.assertEqual(selected[0]['start'], .001)

    def test_idle_and_horizon_cancellation(self):
        s = self.scheduler(50)
        self.assertEqual(s.advance(4), [])
        s.open(command(), 4)
        events = s.advance(4)
        self.assertEqual(events[0]['start'], 4)
        cancelled = s.cancel(4.0005)
        self.assertEqual(sum(e['payload_bytes'] for e in cancelled), 6000)
        self.assertEqual(s.advance(10), [])
        self.assertEqual(s.queue_bytes, 0)

    def test_queue_budget_fails_instead_of_changing_rate(self):
        s = self.scheduler(50, 2500)
        s.open(command(), 0)
        with self.assertRaisesRegex(ValueError, 'queue'):
            s.advance(1)

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

    def test_reject_invalid_rate_delay_and_controls(self):
        for rate, delay in ((0, 1), (-1, 1), (float('inf'), 1), (8, -1), (8, float('nan'))):
            with self.subTest(rate=rate, delay=delay), self.assertRaises(ValueError):
                self.module.IdealScheduler(self.module.ManifestIndex(self.manifest), rate, delay)
        s = self.scheduler()
        bad = command()
        bad['fetches'][0]['tile_id'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'identity'):
            s.open(bad, 0)
        s.open(command(), 0)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            s.open(command(), 0)
        with self.assertRaisesRegex(ValueError, 'request'):
            s.update(dict(updates=[dict(request_id=999, priority=0)]), 0)

    def test_empty_layer_completes_and_object_order_is_numeric(self):
        data = json.loads(self.manifest.read_text())
        layer = data['tiles'][0]['refinements'][0]
        layer['objects'].reverse()
        self.manifest.write_text(json.dumps(data))
        s = self.scheduler()
        s.open(command(1), 0)
        self.assertEqual([e['object_id'] for e in s.advance(1) if e['event'] == 'arrival'], [0, 1])
        (self.root / '0.bin').write_bytes(b'')
        layer.update(objects=[], bytes=0, sha256=hashlib.sha256(b'').hexdigest())
        self.manifest.write_text(json.dumps(data))
        s = self.scheduler()
        self.assertEqual(s.open(command(1), 0)[1]['state'], 'complete')
        self.assertEqual(s.advance(1), [])

    def test_stdio_horizon_close_cancels_without_data_or_failure(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps(dict(manifest=str(self.manifest), output=str(self.root / 'out'), bandwidth_mbps=.008, data_delay_ms=50)))
        p = subprocess.Popen([sys.executable, str(ROOT / 'tools/l4s/ideal_link.py'), '--config', str(config)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertEqual(self.frame(p)[0]['message']['type'], 'ready')
        p.stdin.write((json.dumps(command()) + '\n').encode())
        for _ in range(2):
            self.assertEqual(self.frame(p)[0]['message']['type'], 'fetch-opened')
        p.stdin.write(b'{"type":"close"}\n')
        self.assertEqual(self.frame(p)[0]['message']['type'], 'audit-complete')
        self.assertEqual(p.wait(timeout=3), 0, p.stderr.read().decode())
        self.assertEqual(p.stdout.read(), b'')
        summary = json.loads((self.root / 'out/ideal-summary.json').read_text())
        self.assertEqual(summary['status'], 'PASS')
        self.assertEqual(summary['delivered_objects'], 0)
        for pipe in (p.stdin, p.stdout, p.stderr): pipe.close()

    def test_stdio_queue_violation_records_fail(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps(dict(manifest=str(self.manifest), output=str(self.root / 'out'), bandwidth_mbps=8, data_delay_ms=1000, max_queue_bytes=2500)))
        p = subprocess.Popen([sys.executable, str(ROOT / 'tools/l4s/ideal_link.py'), '--config', str(config)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.frame(p)
        p.stdin.write((json.dumps(command()) + '\n').encode())
        p.wait(timeout=3)
        p.communicate(timeout=3)
        self.assertEqual(p.returncode, 1)
        summary = json.loads((self.root / 'out/ideal-summary.json').read_text())
        self.assertEqual(summary['status'], 'FAIL')
        self.assertEqual(summary['budget_violations'], 1)

    def test_blocked_output_does_not_fail_intact_delivery(self):
        self.manifest = fixture(self.root, sizes=(65536,), tracks=1)
        config = self.root / 'config.json'
        config.write_text(json.dumps(dict(manifest=str(self.manifest), output=str(self.root / 'out'), bandwidth_mbps=800, data_delay_ms=0)))
        p = subprocess.Popen([sys.executable, str(ROOT / 'tools/l4s/ideal_link.py'), '--config', str(config)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.frame(p)
        p.stdin.write((json.dumps(command(1)) + '\n').encode())
        self.assertEqual(self.frame(p)[0]['message']['type'], 'fetch-opened')
        time.sleep(.08)
        header, payload = self.frame(p)
        self.assertEqual(header['wire'], 'object')
        self.assertEqual(payload, bytes([1]) * 65536)
        self.assertEqual(self.frame(p)[0]['message']['type'], 'fetch-status')
        p.stdin.write(b'{"type":"close"}\n')
        self.frame(p)
        p.wait(timeout=3)
        self.assertEqual(p.returncode, 0)
        summary = json.loads((self.root / 'out/ideal-summary.json').read_text())
        self.assertEqual(summary['status'], 'PASS')
        self.assertFalse(any('lateness' in key for key in summary))
        self.assertEqual(summary['delivered_objects'], 1)
        for pipe in (p.stdin, p.stdout, p.stderr): pipe.close()

    def test_stdio_compact_priority_acknowledgements_preserve_every_update(self):
        config = self.root / 'compact.json'
        config.write_text(json.dumps(dict(manifest=str(self.manifest), output=str(self.root / 'out'), bandwidth_mbps=.008, data_delay_ms=500, priority_control_batch_size=64)))
        p = subprocess.Popen([sys.executable, str(ROOT / 'tools/l4s/ideal_link.py'), '--config', str(config)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.frame(p)
        p.stdin.write((json.dumps(command()) + '\n').encode())
        self.frame(p); self.frame(p)
        p.stdin.write((json.dumps(dict(type='priorities', epoch=3, updates=[dict(request_id=1, priority=2, priority_score=2.1), dict(request_id=2, priority=1, priority_score=1.2)])) + '\n').encode())
        message = self.frame(p)[0]['message']
        self.assertEqual(message['type'], 'priorities-accepted')
        self.assertEqual([(u['request_id'],u['new_priority_score']) for u in message['updates']], [(1,2.1),(2,1.2)])
        p.stdin.write(b'{"type":"close"}\n')
        self.assertEqual(self.frame(p)[0]['message']['type'], 'audit-complete')
        self.assertEqual(p.wait(timeout=3), 0)
        events = [json.loads(line) for line in (self.root/'out/ideal-events.jsonl').read_text().splitlines()]
        accepted = [e for e in events if e['event']=='control' and e['message']['type']=='priority-accepted']
        self.assertEqual(len(accepted), 2)
        for pipe in (p.stdin,p.stdout,p.stderr): pipe.close()

    def frame(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 3)[0], 'peer frame timeout')
        prefix = process.stdout.read(4)
        self.assertEqual(len(prefix), 4)
        def exact(size):
            data = bytearray()
            while len(data) < size:
                block = process.stdout.read(size - len(data))
                self.assertTrue(block, 'truncated peer frame')
                data.extend(block)
            return bytes(data)
        header = json.loads(exact(struct.unpack('!I', prefix)[0]))
        payload = exact(header.get('payload_bytes', 0))
        return header, payload

    def test_stdio_controls_payloads_and_summary(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps(dict(manifest=str(self.manifest), output=str(self.root / 'out'), bandwidth_mbps=.08, data_delay_ms=40)))
        p = subprocess.Popen([sys.executable, str(ROOT / 'tools/l4s/ideal_link.py'), '--config', str(config)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertEqual(self.frame(p)[0]['message']['type'], 'ready')
        p.stdin.write((json.dumps(command(1)) + '\n').encode())
        self.assertEqual(self.frame(p)[0]['message']['type'], 'fetch-opened')
        p.stdin.write((json.dumps(dict(type='priorities', epoch=2, updates=[dict(request_id=1, priority=0, priority_score=.25)])) + '\n').encode())
        self.assertEqual(self.frame(p)[0]['message']['type'], 'priority-accepted')
        received = []
        while True:
            header, payload = self.frame(p)
            if header.get('wire') == 'object':
                received.append((header, payload))
            elif header['message']['type'] == 'fetch-status': break
        self.assertEqual([p for h, p in received], [bytes([1]) * 1000, bytes([2]) * 2000])
        self.assertEqual(received[1][0]['subscriber_score'], .25)
        for header, _ in received:
            self.assertGreater(header['ideal_arrival_monotonic_us'], 0)
            self.assertGreater(header['ideal_arrival_unix_ms'], 0)
        p.stdin.write(b'{"type":"close"}\n')
        self.assertEqual(self.frame(p)[0]['message']['type'], 'audit-complete')
        self.assertEqual(p.wait(timeout=3), 0, p.stderr.read().decode())
        summary = json.loads((self.root / 'out/ideal-summary.json').read_text())
        self.assertEqual(summary['delivered_objects'], 2)
        self.assertGreater(summary['clock_origin_unix_ms'], 0)
        self.assertGreater(summary['clock_origin_monotonic_us'], 0)
        self.assertAlmostEqual(received[1][0]['ideal_arrival_unix_ms'] - received[0][0]['ideal_arrival_unix_ms'], 200, places=3)
        self.assertAlmostEqual(received[1][0]['ideal_arrival_monotonic_us'] - received[0][0]['ideal_arrival_monotonic_us'], 200000, delta=1)
        self.assertLessEqual(summary['peak_queue_bytes'], 2**24)
        self.assertEqual(summary['status'], 'PASS')
        for pipe in (p.stdin, p.stdout, p.stderr): pipe.close()


if __name__ == '__main__':
    unittest.main()
