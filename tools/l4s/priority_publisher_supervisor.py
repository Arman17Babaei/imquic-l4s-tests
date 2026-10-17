#!/usr/bin/env python3
"""Keep a persistent player's publisher available across connection loss."""
from __future__ import annotations

import argparse
import signal
import subprocess
import threading
import time
from pathlib import Path


def supervise(command: list[str], ready_file: Path, stop: threading.Event,
              restart_delay: float = 1.0, ready_timeout: float = 180.0) -> None:
    while not stop.is_set():
        ready_file.unlink(missing_ok=True)
        child = subprocess.Popen(command)
        started = time.monotonic()
        ready = False
        try:
            while child.poll() is None and not stop.wait(0.1):
                ready = ready or ready_file.exists()
                if not ready and time.monotonic() - started >= ready_timeout:
                    print("publisher supervisor: readiness timed out", flush=True)
                    break
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            ready_file.unlink(missing_ok=True)
        if not stop.is_set():
            print(f"publisher supervisor: child exited {child.returncode}; restarting", flush=True)
            stop.wait(restart_delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--restart-delay", type=float, default=1.0)
    parser.add_argument("--ready-timeout", type=float, default=180.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.restart_delay < 0 or args.ready_timeout <= 0:
        parser.error("command and valid restart/readiness intervals are required")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    supervise(command, args.ready_file, stop, args.restart_delay, args.ready_timeout)


if __name__ == "__main__":
    main()
