import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from priority_publisher_supervisor import supervise


class PublisherSupervisorTests(unittest.TestCase):
    def run_case(self, publish_ready: bool, first_exits: bool):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, attempts = root / 'ready.json', root / 'attempts'
            ready.write_text('stale')
            program = '''import pathlib,sys,time
ready,attempts=map(pathlib.Path,sys.argv[1:3])
assert not ready.exists(), 'stale readiness reached a replacement publisher'
with attempts.open('a') as f: f.write('1\\n')
count=len(attempts.read_text().splitlines())
if sys.argv[3]=='True': ready.write_text('{}')
if sys.argv[4]=='True' and count==1: sys.exit(23)
time.sleep(3600)
'''
            stop = threading.Event()
            worker = threading.Thread(target=supervise, args=(
                [sys.executable, '-c', program, str(ready), str(attempts),
                 str(publish_ready), str(first_exits)], ready, stop, 0.02, 0.2))
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if attempts.exists() and len(attempts.read_text().splitlines()) >= 2:
                        if not publish_ready or ready.exists():
                            break
                    time.sleep(0.02)
                self.assertGreaterEqual(len(attempts.read_text().splitlines()), 2)
            finally:
                stop.set()
                worker.join(7)
            self.assertFalse(worker.is_alive())
            self.assertFalse(ready.exists())

    def test_restarts_exited_publisher_and_clears_readiness(self):
        self.run_case(True, True)

    def test_restarts_publisher_that_never_becomes_ready(self):
        self.run_case(False, False)


if __name__ == '__main__':
    unittest.main()
