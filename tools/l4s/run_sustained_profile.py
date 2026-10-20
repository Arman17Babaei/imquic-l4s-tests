#!/usr/bin/env python3
"""Prepare and run the current 20Mbps/step5ms Prague coexistence profile."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = Path(__file__).resolve().parent / 'sustained_profile'


def prepare(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    for source in BUNDLE.iterdir():
        if source.is_file():
            shutil.copy2(source, output / source.name)
    for name, source in {
        'prague.c': ROOT / 'deps/picoquic/picoquic/prague.c',
        'picoquic_internal.h': ROOT / 'deps/picoquic/picoquic/picoquic_internal.h',
        'thesis_player_topology.py': ROOT / 'tools/l4s/thesis_player_topology.py',
    }.items():
        shutil.copy2(source, output / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'results' /
                        ('sustained-profile-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
    parser.add_argument('--prepare-only', action='store_true', help='Stage inputs without starting QEMU')
    args = parser.parse_args()
    output = args.output.resolve()
    prepare(output)
    if args.prepare_only:
        print(output)
        return
    subprocess.run([sys.executable, str(output / 'launch.py')], cwd=ROOT, check=True)
    match = re.search(r'QEMU .*: PASS \(([^\n)]+)\)', (output / 'qemu.log').read_text())
    if not match:
        raise RuntimeError('Completed guest result path missing')
    guest = Path(match[1])
    subprocess.run([sys.executable, str(output / 'analyze.py'), '--guest', str(guest),
                    '--output', str(output)], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(output / 'validate_priority.py'), str(guest)],
                   cwd=ROOT, check=True)
    (output / 'complete.json').write_text(json.dumps({'status': 'PASS', 'guest': str(guest)}, indent=2) + '\n')
    print(f'PASS: {output}')


if __name__ == '__main__':
    main()
