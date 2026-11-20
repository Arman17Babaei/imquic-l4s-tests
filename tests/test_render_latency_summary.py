import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from summarize_render_latency import summarize


class RenderLatencyTests(unittest.TestCase):
    def test_ignores_progress_and_does_not_invent_missing_stage_timings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'reference-browser.log').write_text('BROWSER message\n' +
                '\n'.join(json.dumps(value) for value in [
                    {'frames': 20}, {'referenceFrame': 0, 'frameWallMs': 100,
                                     'browserCaptureMs': 60},
                    {'referenceFrame': 1, 'frameWallMs': 200, 'browserCaptureMs': 80}]))
            result = summarize(root)
            self.assertEqual(result['frames'], 2)
            stages = result['shards'][0]['stages']
            self.assertEqual(stages['frameWallMs']['median_ms'], 150)
            self.assertEqual(stages['browserCaptureMs']['sum_ms'], 140)
            self.assertNotIn('browserSeekMs', stages)
