"""Reference CLI safety and provenance tests; no browser or transport runs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('render_trace_reference',
    Path(__file__).resolve().parents[1] / 'tools/l4s/render_trace_reference.py')
REFERENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REFERENCE)


class TraceReferenceTests(unittest.TestCase):
    def test_process_descendants_include_changed_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Parent IDs, not process groups, define ownership.
            for pid, parent in ((10, 1), (11, 10), (12, 11), (20, 1)):
                (root / str(pid)).mkdir()
                (root / str(pid) / 'stat').write_text(f'{pid} (name with spaces) S {parent} 0')
            original_path = Path
            with patch.object(REFERENCE, 'Path', side_effect=lambda value: root if value == '/proc' else original_path(value)):
                self.assertEqual(REFERENCE.process_descendants(10), {11, 12})

    def test_provenance_hashes_assets_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'manifest.json'
            trace = root / 'trace.json'
            trace.write_text('[{"timestamp_ms":0},{"timestamp_ms":100}]')
            (root / 'payload.gsp2').write_bytes(b'payload')
            manifest.write_text(json.dumps({'tiles': [{'refinements': [{'file': 'payload.gsp2'}]}]}))
            evidence = REFERENCE.input_provenance(manifest, trace)
            self.assertEqual(evidence['assets'][0]['sha256'],
                '239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5')
            manifest.write_text(json.dumps({'tiles': [{'refinements': [{'file': '../outside'}]}]}))
            with self.assertRaises(ValueError):
                REFERENCE.input_provenance(manifest, trace)

    def test_ui_snapshot_excludes_bundled_manifest_that_would_override_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / 'dist'
            (dist / 'assets').mkdir(parents=True)
            (dist / 'index.html').write_text('<html>player</html>')
            (dist / 'assets/main.js').write_text('player')
            (dist / 'manifest-v2.json').write_text('{"wrongScene":true}')
            target = root / 'player-ui'
            REFERENCE.snapshot_player_ui(dist, target)
            self.assertTrue((target / 'index.html').is_file())
            self.assertTrue((target / 'assets/main.js').is_file())
            self.assertFalse((target / 'manifest-v2.json').exists())

    def test_output_must_be_new_and_under_results(self):
        with self.assertRaises(ValueError):
            REFERENCE.validate_output(Path('/tmp/reference-output'))
        with self.assertRaises(FileExistsError):
            REFERENCE.validate_output(REFERENCE.ROOT / 'results')
