"""Exercise owned process teardown with real subprocesses, without Mininet."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/l4s'))


class GuestCleanupTests(unittest.TestCase):
    def test_teardown_kills_descendant_even_when_parent_ignores_term(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/"tools/l4s/guest_cleanup.py").exists(), "guest cleanup helper is required")
        import guest_cleanup as cleanup
        with tempfile.TemporaryDirectory() as temp:
            marker=Path(temp)/'child'
            program='import subprocess,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); p=subprocess.Popen(["sleep","60"]); open('+repr(str(marker))+',"w").write(str(p.pid)); time.sleep(60)'
            p=subprocess.Popen([sys.executable,'-c',program],start_new_session=True)
            try:
                deadline=time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(marker.exists())
                record=cleanup.process_record(p.pid,group=True)
                cleanup.stop_record(record,timeout=.1)
                p.wait(timeout=3)
                child=int(marker.read_text())
                self.assertFalse(cleanup.alive(child))
            finally:
                try:os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                p.wait()

    def test_recycled_pid_record_does_not_kill_other_process(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/"tools/l4s/guest_cleanup.py").exists(), "guest cleanup helper is required")
        import guest_cleanup as cleanup
        p=subprocess.Popen(['sleep','60'],start_new_session=True)
        try:
            record=cleanup.process_record(p.pid,group=True);record['start_ticks']-=1
            cleanup.stop_record(record,timeout=.1)
            self.assertIsNone(p.poll())
        finally:p.kill();p.wait()

    def test_ownership_is_published_before_cleanup_and_contains_no_credentials(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/"tools/l4s/guest_cleanup.py").exists(), "guest cleanup helper is required")
        import guest_cleanup as cleanup
        with tempfile.TemporaryDirectory() as temp:
            p=subprocess.Popen(['sleep','60'],start_new_session=True)
            try:
                state=cleanup.Ownership(Path(temp)/'ownership.json',[])
                state.add(p.pid,group=True)
                stored=json.loads(state.path.read_text())
                self.assertEqual(stored['processes'][0]['pid'],p.pid)
                self.assertEqual(stored['runner']['pid'],os.getpid())
            finally:p.kill();p.wait()

class GuestResetTests(unittest.TestCase):
    def test_dirty_namespace_causes_reset_failure_and_retains_evidence(self):
        from unittest.mock import patch
        import guest_cleanup as cleanup
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);evidence=root/'reset.json'
            dirty={'extra_network_namespaces':{'net:[123]':[999]},'bridges':[],
                   'test_interfaces':[],'test_listeners':[]}
            with patch.object(cleanup,'idle_state',return_value=dirty),patch.object(cleanup.subprocess,'run',return_value=type('Result',(),{'returncode':0,'stdout':''})()):
                with self.assertRaisesRegex(RuntimeError,'left network state'):
                    cleanup.reset(root/'missing-ownership.json',evidence)
            self.assertEqual(json.loads(evidence.read_text())['status'],'FAIL')
