"""Session metadata and ownership lifecycle without booting QEMU."""
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/l4s'))
from guest_cleanup import process_record, alive
from qemu_session import read_session, SuiteSession


class QemuSessionTests(unittest.TestCase):
    def test_dead_or_recycled_owner_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'session.json'
            owner=process_record(os.getpid());owner['start_ticks']-=1
            path.write_text(json.dumps({'owner':owner}))
            with self.assertRaisesRegex(RuntimeError,'no longer running'):read_session(path)

    def test_close_reaps_only_the_session_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            session=SuiteSession({'output':temp})
            session.path.parent.mkdir(parents=True);session.path.write_text('{}')
            session.process=subprocess.Popen(['sleep','60'],start_new_session=True)
            session.close()
            self.assertIsNotNone(session.process.poll())
            self.assertFalse(session.path.exists())

    def test_close_removes_vm_descendant_after_owner_was_hard_killed(self):
        with tempfile.TemporaryDirectory() as temp:
            marker=Path(temp)/'child'
            program='import subprocess,time; p=subprocess.Popen(["sleep","60"]); open('+repr(str(marker))+',"w").write(str(p.pid)); time.sleep(60)'
            session=SuiteSession({'output':temp})
            session.process=subprocess.Popen([sys.executable,'-c',program],start_new_session=True)
            session.owner_record=process_record(session.process.pid,group=True)
            try:
                deadline=time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(marker.exists())
                child=int(marker.read_text());session.process.kill();session.process.wait()
                session.close()
                self.assertFalse(alive(child),'VM descendant survived its dead owner')
            finally:
                try:os.killpg(session.process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                session.process.wait()
