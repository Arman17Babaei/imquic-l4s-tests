"""SIGTERM must unwind the guest lifecycle instead of skipping finally."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

TOOLS=Path(__file__).resolve().parents[1]/'tools/l4s'


class GuestSignalTests(unittest.TestCase):
    def test_sigterm_executes_finally(self):
        with tempfile.TemporaryDirectory() as temp:
            ready=Path(temp)/'ready';done=Path(temp)/'done'
            program=f'''import sys,time\nfrom pathlib import Path\nsys.path.insert(0,{str(TOOLS)!r})\nfrom run_camera_player_guest import install_signal_handlers\ninstall_signal_handlers()\ntry:\n Path({str(ready)!r}).touch()\n time.sleep(60)\nfinally:\n Path({str(done)!r}).touch()\n'''
            p=subprocess.Popen([sys.executable,'-c',program],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and p.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(ready.exists(),p.stderr.read().decode() if p.poll() is not None else 'runner failed to become ready')
                p.send_signal(signal.SIGTERM);p.wait(timeout=5)
                self.assertTrue(done.exists(),'SIGTERM skipped guest teardown')
            finally:
                if p.poll() is None:p.kill();p.wait()
                p.stderr.close()
