from pathlib import Path
import tempfile
import tarfile
import json
import unittest
import sys
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
if __name__=='__main__':unittest.main()
