from pathlib import Path
import tempfile
import tarfile
import json
import unittest
import sys
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools/l4s'))
from run_network_capture import adapter_archive
class AdapterArchiveTest(unittest.TestCase):
    def test_staged_adapter_keeps_its_commonjs_module_boundary(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'results') as folder:
            target=Path(folder)/'adapter.tar';adapter_archive(target)
            with tarfile.open(target) as archive:
                self.assertEqual(json.load(archive.extractfile('package.json'))['type'],'commonjs')
                self.assertIsNotNone(archive.getmember('dist-headless/network-capture-client.js'))
                self.assertIsNotNone(archive.getmember('node_modules/three/package.json'))

class ResourceForwardingTest(unittest.TestCase):
    def test_capture_forwards_resource_flags_and_records_them(self):
        import run_network_capture as runner
        with tempfile.TemporaryDirectory(dir=ROOT/'results') as folder:
            root = Path(folder)
            manifest = root/'manifest.json'
            manifest.write_text('{}')
            output = root/'capture'
            argv = ['capture', '--manifest', str(manifest), '--trace', str(root/'trace'),
                    '--output', str(output), '--controller', 'prague',
                    '--host-cpus', '2-5', '--host-numa-node', '0',
                    '--preallocate-memory', '--lock-memory']
            with patch.object(sys, 'argv', argv), \
                 patch.object(runner, 'validate_manifest'), \
                 patch.object(runner, 'validate_host_resources'), \
                 patch.object(runner, 'scale_trace', return_value={}), \
                 patch.object(runner, 'scene_archive'), \
                 patch.object(runner, 'adapter_archive'), \
                 patch.object(runner.subprocess, 'Popen', side_effect=RuntimeError('stop before launch')):
                with self.assertRaisesRegex(RuntimeError, 'stop before launch'):
                    runner.main()
            record = json.loads((output/'provenance.json').read_text())
            for flag in ('--host-cpus', '--host-numa-node', '--preallocate-memory', '--lock-memory'):
                self.assertIn(flag, record['command'])
            self.assertEqual(record['host_resources'], dict(host_cpus='2-5', host_numa_node=0,
                preallocate_memory=True, lock_memory=True))

if __name__=='__main__':unittest.main()
