"""Real IMQUIC object stepping; no network shaping or timing-derived results."""
import importlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
import struct
import json
import hashlib
from fractions import Fraction

from tests.test_ideal_link import fixture, command

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/l4s'))


def player_fixture(root, encoding='gsp2'):
    """Two native GSP3 objects and an empty-start moving camera trace."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    objects = []
    for i in range(2):
        arrays = [struct.pack('<I', i), struct.pack('<3f', .2 if i else -.2, 0, -3),
                  struct.pack('<f', 0), struct.pack('<48f', *[(i+1)*.01*(j%3+1) for j in range(48)]),
                  struct.pack('<3f', -2,-2,-2), struct.pack('<4f', 1,0,0,0)]
        body = b'tile0' + b'0' + b''.join(struct.pack('<I', len(a)) + a for a in arrays)
        objects.append(struct.pack('<10I', 0x47535033,2,0,5,1,0,i,1,3,len(body)) + body)
    if encoding == 'splat':
        objects = [struct.pack('<6f8B', .2 if i else -.2,0,-3,.1,.1,.1,255,100,50,255,255,128,128,128) for i in range(2)]
    content = b''.join(objects)
    (root/'data.gsp2').write_bytes(content)
    digest = lambda data: hashlib.sha256(data).hexdigest()
    layers = [dict(index=0,name='base',file='data.gsp2',track='tiny/tile0/layer0',gaussian_count=2,
        bytes=len(content), object_count=2,object_bytes=len(objects[0]),fraction=1,sha256=digest(content),
        objects=[dict(id=i,offset=i*len(obj),bytes=len(obj),gaussian_count=1,publisher_priority=0,sha256=digest(obj))
                 for i,obj in enumerate(objects)])]
    for i in range(1,4):
        (root/f'empty{i}.gsp2').write_bytes(b'')
        layers.append(dict(index=i,name=f'layer{i}',file=f'empty{i}.gsp2',track=f'tiny/tile0/layer{i}',
            gaussian_count=0,bytes=0,object_count=0,object_bytes=0,fraction=0,sha256=digest(b''),objects=[]))
    manifest = dict(schema_version=2,payload_encoding=encoding,coordinate_system='source',
        scene=dict(id='tiny',sampling='full',source_sha256=digest(content),gaussian_count=2,splat_size=0,
                   min=[-1,-1,-4],max=[1,1,-2],center=[0,0,-3],radius=2),
        tiling=dict(type='uniform-aabb-grid',dimensions=[1,1,1],occupied_tiles=1),
        progression=dict(ordering='source',layer_fractions=[1,0,0,0],cumulative_fractions=[1,1,1,1]),
        tiles=[dict(id='tile0',grid_index=[0,0,0],min=[-1,-1,-4],max=[1,1,-2],centroid=[0,0,-3],
                    gaussian_count=2,refinements=layers)])
    (root/'manifest-v2.json').write_text(json.dumps(manifest))
    (root/'trace.json').write_text(json.dumps([dict(timestamp_ms=t,camera_position=[t/9000,0,0],
        camera_forward=[0,0,-1],camera_up=[0,1,0],yaw_degrees=0,fov=60,aspect_ratio=16/9) for t in (0,700)]))


class ImquicSteppingTests(unittest.TestCase):
    def test_selection_at_frame_boundary_precedes_that_frames_priority_update(self):
        module = importlib.import_module('ideal_link')
        with tempfile.TemporaryDirectory(dir=ROOT / 'results') as temporary:
            root = Path(temporary)
            manifest = fixture(root)
            with module.ImquicPeer(manifest, root) as peer:
                model = module.OfflineDelivery(peer, 8, 0)
                opened = command()
                opened['fetches'][1]['priority'] = 20
                controls = model.open(opened)
                request = next(e['request_id'] for e in controls if e['type']=='fetch-opened' and e['tile_id']=='1')
                events = model.advance(Fraction(1,1000))
                self.assertEqual([(e['tile_id'],e['object_id']) for e in events if e['event']=='serialization'],
                                 [('0',0),('0',1)])
                model.update(dict(type='priorities',epoch=1,updates=[dict(request_id=request,priority=0)]))
                events = model.advance(Fraction(4,1000))
                self.assertEqual([(e['tile_id'],e['object_id']) for e in events if e['event']=='arrival'],
                                 [('0',1),('1',0)])

    def test_real_fetch_priority_updates_and_host_wait_independence(self):
        self.assertTrue((ROOT / 'build/priority-moq-sidecar').is_file())
        module = importlib.import_module('ideal_link')
        self.assertTrue(hasattr(module, 'ImquicPeer'), 'ideal must use an actual stepped IMQUIC peer')
        schedules = []
        for wait in (0, .05):
            with tempfile.TemporaryDirectory(dir=ROOT / 'results') as temporary:
                root = Path(temporary)
                manifest = fixture(root)
                with module.ImquicPeer(manifest, root) as peer:
                    opened = peer.open(command())
                    requests = {e['tile_id']: e['request_id'] for e in opened if e['type'] == 'fetch-opened'}
                    time.sleep(wait)
                    first, payload = peer.next_object()
                    self.assertEqual((first['tile_id'], first['object_id'], len(payload)), ('0', 0, 1000))
                    peer.update(dict(type='priorities', epoch=1, updates=[dict(request_id=requests['1'], priority=0)]))
                    records = [first]
                    while True:
                        time.sleep(wait)
                        item = peer.next_object()
                        if item is None:
                            break
                        header, payload = item
                        records.append(header)
                        self.assertEqual(len(payload), header['payload_bytes'])
                    schedules.append([(e['tile_id'], e['object_id'], e['subscriber_priority']) for e in records])
                    self.assertEqual(schedules[-1], [('0', 0, 10), ('1', 0, 0), ('1', 1, 0), ('0', 1, 10)])
        self.assertEqual(*schedules)

    def test_virtual_bytes_delay_boundary_and_no_idle_credit(self):
        module = importlib.import_module('ideal_link')
        self.assertTrue(hasattr(module, 'OfflineDelivery'), 'offline byte accounting is required')
        with tempfile.TemporaryDirectory(dir=ROOT / 'results') as temporary:
            root = Path(temporary)
            manifest = fixture(root, sizes=(1000, 2000), tracks=1)
            with module.ImquicPeer(manifest, root) as peer:
                model = module.OfflineDelivery(peer, 8, 10)
                self.assertEqual(model.advance(Fraction(1)), [])
                model.open(command(tracks=1))
                self.assertEqual([e for e in model.advance(Fraction(10005, 10000)) if e['event'] == 'arrival'], [])
                events = model.advance(Fraction(1011, 1000))
                arrivals = [e for e in events if e['event'] == 'arrival']
                self.assertEqual([(e['object_id'], e['arrival']) for e in arrivals], [(0, 1.011)])
                arrivals = [e for e in model.advance(Fraction(1013, 1000)) if e['event'] == 'arrival']
                self.assertEqual([(e['object_id'], e['arrival']) for e in arrivals], [(1, 1.013)])
                with self.assertRaises(ValueError):
                    model.advance(Fraction(1))
