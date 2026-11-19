import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/l4s'))
from arrival_replay import ArrivalReplay
from tests.test_ideal_imquic import player_fixture

ROOT=Path(__file__).resolve().parents[1]
class ArrivalReplayTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'results');self.root=Path(self.temp.name)
        player_fixture(str(self.root),'gsp2');self.manifest=self.root/'manifest-v2.json'
        data=json.loads(self.manifest.read_text());self.records=[]
        for tile in data['tiles']:
            for layer in tile['refinements']:
                for obj in layer['objects']:
                    self.records.append(dict(tile_id=tile['id'],refinement=layer['index'],object_id=obj['id'],payload_bytes=obj['bytes'],actual_sha256=obj['sha256'],arrival_us=300000+len(self.records)*100000))
        self.journal=self.root/'arrivals.json'
        self.write()
    def tearDown(self):self.temp.cleanup()
    def write(self):
        self.journal.write_text(json.dumps(dict(status='PASS',clock='guest-monotonic',duration_ms=700,manifest_sha256=hashlib.sha256(self.manifest.read_bytes()).hexdigest(),objects=self.records)))
    def test_no_future_objects_and_slow_wall_clock_does_not_advance(self):
        replay=ArrivalReplay(self.manifest,self.journal)
        self.assertEqual(replay.advance(299999),[])
        first=replay.advance(300000);self.assertEqual(len(first),1)
        self.assertEqual(first[0][0]['recorded_arrival_us'],300000)
        self.assertEqual(replay.advance(300000),[])
        with self.assertRaises(ValueError):replay.advance(200000)
        remaining=replay.advance(700000);self.assertEqual(len(first)+len(remaining),len(self.records))
    def test_identity_and_extent_are_verified(self):
        self.records[0]['actual_sha256']='0'*64;self.write()
        with self.assertRaisesRegex(ValueError,'hash'):ArrivalReplay(self.manifest,self.journal)
    def test_reject_duplicate_and_out_of_horizon(self):
        self.records.append(self.records[0]);self.write()
        with self.assertRaisesRegex(ValueError,'duplicate'):ArrivalReplay(self.manifest,self.journal)
        self.records=self.records[:1];self.records[0]['arrival_us']=700001;self.write()
        with self.assertRaisesRegex(ValueError,'horizon'):ArrivalReplay(self.manifest,self.journal)
if __name__=='__main__':unittest.main()
