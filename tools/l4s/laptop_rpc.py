#!/usr/bin/env python3
"""Explicitly enabled, token-authenticated laptop control over loopback SSH forwarding.

Commands run with the server user's privileges. This is NOT a command sandbox.
Never bind this service publicly or run it as root.
"""
from __future__ import annotations

import argparse
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import threading
import urllib.request

MAX_REQUEST = 8 * 1024 * 1024
MAX_CHUNK = 1024 * 1024


def load_token(path: Path) -> str:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('token must be a regular, non-symlink file')
    if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise ValueError('token must be owned by this user with mode 0600')
    token = path.read_text().strip()
    if len(token) < 32:
        raise ValueError('token must contain at least 32 characters')
    return token


class Workspace:
    def __init__(self, root: Path, state: Path):
        self.root = root.resolve(strict=True)
        self.state = state.resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs = {}
        self.lock = threading.Lock()

    def path(self, value: str) -> Path:
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('path escapes workspace')
        return path

    def dispatch(self, method: str, params: dict) -> dict:
        if method == 'info':
            return {'root': str(self.root), 'pid': os.getpid(), 'cpu_count': os.cpu_count(),
                    'uid': os.getuid(), 'capabilities': ['list', 'read', 'write', 'start', 'status', 'stop']}
        if method == 'list':
            path = self.path(params.get('path', '.'))
            entries = sorted(path.iterdir())
            if len(entries) > 10000:
                raise ValueError('directory too large')
            return {'entries': [{'name': p.name, 'symlink': p.is_symlink(),
                                 'directory': p.is_dir()} for p in entries]}
        if method == 'read':
            path = self.path(params['path'])
            offset, size = int(params.get('offset', 0)), int(params.get('size', MAX_CHUNK))
            if offset < 0 or not 0 <= size <= MAX_CHUNK:
                raise ValueError('invalid read range')
            with path.open('rb') as stream:
                stream.seek(offset)
                data = stream.read(size)
            return {'data_base64': base64.b64encode(data).decode(), 'bytes': len(data),
                    'offset': offset, 'file_size': path.stat().st_size}
        if method == 'write':
            path = self.path(params['path'])
            data = base64.b64decode(params['data_base64'], validate=True)
            if len(data) > MAX_CHUNK:
                raise ValueError('write chunk too large')
            # Default is exclusive creation: replacing existing work must be explicit.
            mode = 'wb' if params.get('overwrite') is True else 'xb'
            with path.open(mode) as stream:
                stream.write(data)
            return {'bytes': len(data)}
        if method == 'start':
            argv = params['argv']
            if (not isinstance(argv, list) or not argv or len(argv) > 1024 or
                    any(not isinstance(arg, str) or '\0' in arg for arg in argv)):
                raise ValueError('argv must be a nonempty string array')
            cwd = self.path(params.get('cwd', '.'))
            job = secrets.token_hex(12)
            log = self.state / f'{job}.log'
            with self.lock:
                if sum(p.poll() is None for p in self.jobs.values()) >= 16:
                    raise ValueError('too many running jobs')
                with log.open('xb') as output:
                    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                self.jobs[job] = process
                (self.state / f'{job}.json').write_text(json.dumps(
                    {'job': job, 'argv': argv, 'cwd': str(cwd), 'pid': process.pid}) + '\n')
            return {'job': job, 'pid': process.pid}
        if method in ('status', 'stop'):
            job = params['job']
            with self.lock:
                process = self.jobs[job]
                if method == 'stop' and process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            offset = int(params.get('offset', 0))
            if offset < 0:
                raise ValueError('invalid log offset')
            with (self.state / f'{job}.log').open('rb') as stream:
                stream.seek(offset)
                data = stream.read(MAX_CHUNK)
            return {'job': job, 'pid': process.pid, 'returncode': process.poll(),
                    'log_base64': base64.b64encode(data).decode(), 'next_offset': offset + len(data)}
        raise ValueError('unknown method')


def handler_for(workspace: Workspace, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not log tokens, file contents, or command arguments.

        def do_POST(self):
            self.connection.settimeout(15)
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(401)
                return
            if self.path != '/rpc':
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError('invalid request size')
                request = json.loads(self.rfile.read(length))
                result = {'ok': True, 'result': workspace.dispatch(request['method'], request.get('params', {}))}
                code = 200
            except (ValueError, KeyError, OSError, TypeError) as error:
                result, code = {'ok': False, 'error': str(error)}, 400
            body = json.dumps(result).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', required=True, type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init-token')
    serve = sub.add_parser('serve')
    serve.add_argument('--root', required=True, type=Path)
    serve.add_argument('--state', required=True, type=Path)
    serve.add_argument('--port', type=int, default=18765)
    serve.add_argument('--allow-exec', action='store_true', required=True)
    call = sub.add_parser('call')
    call.add_argument('--port', type=int, default=18766)
    call.add_argument('method')
    call.add_argument('params', nargs='?', default='{}', help='JSON object')
    args = parser.parse_args()
    if args.action == 'init-token':
        fd = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(48) + '\n')
        return
    token = load_token(args.token_file)
    if args.action == 'serve':
        if os.getuid() == 0:
            parser.error('run the laptop server as your normal user, not root')
        workspace = Workspace(args.root, args.state)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(workspace, token))
        print(f'Laptop RPC listening on 127.0.0.1:{args.port}; root={workspace.root}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for process in workspace.jobs.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            server.server_close()
    else:
        body = json.dumps({'method': args.method, 'params': json.loads(args.params)}).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{args.port}/rpc', data=body,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            print(json.dumps(json.load(response), indent=2))


if __name__ == '__main__':
    main()
