"""Trace references are shared across runs, independently of candidate output."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
import render_trace_reference as reference


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='results')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.manifest = self.root / 'manifest.json'
        (self.root / 'payload.gsp2').write_bytes(b'payload')
        self.manifest.write_text(json.dumps({'scene': {'gaussian_count': 1},
            'tiles': [{'refinements': [{'file': 'payload.gsp2'}]}]}))
        self.trace = self.root / 'trace.json'
        self.trace.write_text('[{"timestamp_ms":0},{"timestamp_ms":100}]')
        self.dist = self.root / 'dist'
        (self.dist / 'assets').mkdir(parents=True)
        (self.dist / 'index.html').write_text('player')
        (self.dist / 'assets/main.js').write_text('renderer')
        self.settings = dict(cache_root=self.root / 'cache', manifest=self.manifest,
                             trace=self.trace, dist=self.dist, fps=10, width=960,
                             height=540, gpu_mode='software', headless=True, timeout_seconds=60)

    def render(self, argv):
        output = Path(argv[argv.index('--output') + 1])
        (output / 'reference-frames').mkdir(parents=True)
        (output / 'reference-frames/frame.png').write_bytes(b'fixture')
        pose = dict(viewMatrix=[1], position=[0, 0, 0], up=[0, 1, 0], fov=50, aspect=1)
        frame = dict(index=0, timestampMs=0, file='frame.png', cameraPose=pose, appliedGaussians=1)
        (output / 'reference-frames/frames.json').write_text(json.dumps(
            dict(complete=True, expectedFrames=1, frames=[frame])))
        (output / 'reference-summary.json').write_text('{"complete":true,"appliedGaussians":1}')
        (output / 'reference-telemetry.json').write_text('{}')
        (output / 'reference.mp4').write_bytes(b'video')

    def test_same_trace_contents_reuse_one_render_across_input_paths(self):
        with patch.object(reference, 'main', side_effect=self.render) as render:
            first = reference.cached_trace_reference(**self.settings)
            other = self.root / 'other-trace.json'
            other.write_bytes(self.trace.read_bytes())
            second = reference.cached_trace_reference(**{**self.settings, 'trace': other})
            self.assertEqual(first, second)
            self.assertEqual(render.call_count, 1)
            self.assertNotIn('--reference-frames', render.call_args.args[0])

    def test_trace_scene_renderer_and_viewport_changes_invalidate_cache(self):
        with patch.object(reference, 'main', side_effect=self.render) as render:
            entries = [reference.cached_trace_reference(**self.settings)]
            self.trace.write_text('[{"timestamp_ms":0,"x":1},{"timestamp_ms":100}]')
            entries.append(reference.cached_trace_reference(**self.settings))
            (self.root / 'payload.gsp2').write_bytes(b'changed payload')
            entries.append(reference.cached_trace_reference(**self.settings))
            manifest = json.loads(self.manifest.read_text())
            self.manifest.write_text(json.dumps({**manifest, 'revision': 2}))
            entries.append(reference.cached_trace_reference(**self.settings))
            (self.dist / 'assets/main.js').write_text('updated renderer')
            entries.append(reference.cached_trace_reference(**self.settings))
            entries.append(reference.cached_trace_reference(**{**self.settings, 'width': 1280}))
            self.assertEqual(len(set(entries)), 6)
            self.assertEqual(render.call_count, 6)

    def test_failed_render_is_never_published_or_reused(self):
        with patch.object(reference, 'main', side_effect=RuntimeError('render failed')):
            with self.assertRaisesRegex(RuntimeError, 'render failed'):
                reference.cached_trace_reference(**self.settings)
        with patch.object(reference, 'main', side_effect=self.render) as render:
            entry = reference.cached_trace_reference(**self.settings)
            self.assertTrue((entry / 'reference-frames/frames.json').is_file())
            self.assertEqual(render.call_count, 1)

    def test_missing_cached_pixels_are_rejected(self):
        with patch.object(reference, 'main', side_effect=self.render):
            entry = reference.cached_trace_reference(**self.settings)
        (entry / 'reference-frames/frame.png').unlink()
        with self.assertRaisesRegex(ValueError, 'missing|unsafe'):
            reference.cached_trace_reference(**self.settings)
