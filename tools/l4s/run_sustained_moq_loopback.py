#!/usr/bin/env python3
"""Run a short local publisher/subscriber MoQ smoke test."""

import argparse
import csv
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path,
                        default=Path("deps/imquic/src/imquic-sustained-moq"))
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    if not args.binary.exists():
        raise SystemExit(f"missing {args.binary}; run make build first")
    with tempfile.TemporaryDirectory(prefix="imquic-moq-loopback-") as directory:
        metrics = Path(directory) / "metrics.csv"
        result = Path(directory) / "subscriber-result.json"
        publisher = subprocess.Popen(
            [str(args.binary), "publisher", "127.0.0.1", "4443",
             "l4s-off", str(metrics), "2"],
            cwd=args.binary.parent, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            subscriber = subprocess.Popen(
                [str(args.binary), "subscriber", "127.0.0.1", "4443",
                 "l4s-off", "2", str(result)],
                cwd=args.binary.parent, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            subscriber_output, _ = subscriber.communicate(timeout=10)
            publisher_output, _ = publisher.communicate(timeout=10)
        finally:
            if publisher.poll() is None:
                publisher.kill()
        if publisher.returncode != 0 or subscriber.returncode != 0:
            raise SystemExit(
                "MoQ loopback failed\npublisher:\n"
                f"{publisher_output}\nsubscriber:\n{subscriber_output}")
        with metrics.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            raise SystemExit("MoQ loopback produced no transport metrics")
        if "queued_stream_bytes" not in rows[0]:
            raise SystemExit("MoQ loopback metrics omit queued stream bytes")
        if not any(int(row["queued_stream_bytes"]) > 0 for row in rows):
            raise SystemExit("MoQ publisher never maintained its object buffer")
        if not result.is_file() or not result.read_text(encoding="utf-8").strip():
            raise SystemExit("MoQ loopback produced no subscriber result")
    print("Sustained MoQ loopback: PASS")


if __name__ == "__main__":
    main()
