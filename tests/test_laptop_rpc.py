import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))
from laptop_rpc import Workspace, handler_for, load_token


class LaptopRpcTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root, self.root / 'state')

    def tearDown(self):
        self.temporary.cleanup()

    def test_paths_and_exclusive_writes(self):
        for path in ('../escape', '/etc/passwd'):
            with self.assertRaises(ValueError):
                self.workspace.path(path)
        (self.root / 'outside').symlink_to('/etc')
        with self.assertRaises(ValueError):
            self.workspace.path('outside/passwd')
        params = {'path': 'hello', 'data_base64': base64.b64encode(b'hello').decode()}
        self.workspace.dispatch('write', params)
        with self.assertRaises(FileExistsError):
            self.workspace.dispatch('write', params)
        result = self.workspace.dispatch('read', {'path': 'hello', 'offset': 1, 'size': 3})
        self.assertEqual(base64.b64decode(result['data_base64']), b'ell')

    def test_process_status_and_output(self):
        result = self.workspace.dispatch('start', {'argv': [sys.executable, '-c', 'print("hello")']})
        process = self.workspace.jobs[result['job']]
        self.assertEqual(process.wait(timeout=10), 0)
        status = self.workspace.dispatch('status', {'job': result['job']})
        self.assertEqual(base64.b64decode(status['log_base64']), b'hello\n')
        self.assertEqual(status['returncode'], 0)

    def test_stop_owned_process(self):
        result = self.workspace.dispatch('start', {'argv': [sys.executable, '-c', 'import time;time.sleep(60)']})
        self.workspace.dispatch('stop', {'job': result['job']})
        self.workspace.jobs[result['job']].wait(timeout=10)
        with self.assertRaises(KeyError):
            self.workspace.dispatch('stop', {'job': 'unknown'})

    def test_token_permissions(self):
        token = self.root / 'token'
        token.write_text('x' * 48)
        token.chmod(0o600)
        self.assertEqual(load_token(token), 'x' * 48)
        token.chmod(0o644)
        with self.assertRaises(ValueError):
            load_token(token)

    def test_http_authentication(self):
        token = 'x' * 48
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.workspace, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/rpc'
            body = json.dumps({'method': 'info'}).encode()
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5)
            self.assertEqual(error.exception.code, 401)
            request = urllib.request.Request(url, data=body, headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertTrue(json.load(response)['ok'])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
