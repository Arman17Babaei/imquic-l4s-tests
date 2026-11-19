from pathlib import Path
import tempfile,json,hashlib,copy,sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools/l4s'))
from render_arrival_replay import validate_replay_inputs, audit_frames
class ReplayAuditTest(unittest.TestCase):
 def test_trace_identity_is_required_even_with_same_horizon(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'results') as tmp:
   p=Path(tmp);manifest=p/'manifest.json';manifest.write_text('{}');trace=p/'trace.json'
   frames=[dict(timestamp_ms=t,camera_position=[0,0,0],camera_forward=[0,0,-1],camera_up=[0,1,0],yaw_degrees=0,fov=60,aspect_ratio=1) for t in [0,1000]];trace.write_text(json.dumps(frames))
   journal=p/'arrivals.json';journal.write_text(json.dumps(dict(status='PASS',clock='guest-monotonic',duration_ms=1000,manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest())))
   validate_replay_inputs(manifest,trace,journal)
   frames[0]['camera_position'][0]=1;trace.write_text(json.dumps(frames))
   with self.assertRaisesRegex(ValueError,'trace hash'):validate_replay_inputs(manifest,trace,journal)
 def test_missing_dispatch_or_applied_geometry_cannot_pass(self):
  manifest={'payload_encoding':'gsp2','tiles':[{'id':'tile','refinements':[{'index':0,'objects':[{'id':0,'gaussian_count':2}]}]}]}
  journal={'objects':[{'tile_id':'tile','refinement':0,'object_id':0,'arrival_us':100000}]}
  telemetry={'events':[{'type':'render-frame','timestampMs':200,'objectsAdded':['0:tile:0:0']}]};frames={'frames':[{'timestampMs':200,'appliedGaussians':2}]};replay={'frame_dispatches':[{'frame_index':0,'objects':[['tile',0,0]]}]}
  self.assertEqual(audit_frames(journal,telemetry,frames,manifest,replay)['status'],'PASS')
  missing=copy.deepcopy(replay);missing['frame_dispatches'][0]['objects']=[]
  with self.assertRaisesRegex(ValueError,'dispatch'):audit_frames(journal,telemetry,frames,manifest,missing)
  missing=copy.deepcopy(telemetry);missing['events'][0]['objectsAdded']=[]
  with self.assertRaisesRegex(ValueError,'membership'):audit_frames(journal,missing,frames,manifest,replay)
if __name__=='__main__':unittest.main()
