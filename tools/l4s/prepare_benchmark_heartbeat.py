#!/usr/bin/env python3
"""Prepare an isolated heartbeat experiment from the known player bundle.

This is a guarded diagnostic adapter, not a general minified-source patcher.
The player submodule cannot be fetched in the benchmark environment. Keep the
original build untouched and fail if its exact seek implementation changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from render_trace_reference import ROOT

BEFORE = ('await F.update({scene:j,camera:M}),N.render(j,M),'
          'N.getContext().finish(),I(),w.cameraPose')
AFTER = ('F.autoUpdate=!1,await (async()=>{const u=F.update({scene:j,camera:M});'
         'N.setAnimationLoop(()=>N.render(j,M));try{await u}'
         'finally{N.setAnimationLoop(null);F.autoUpdate=!0}})(),N.render(j,M),'
         'N.getContext().finish(),I(),w.cameraPose')


def prepare(source: Path, output: Path, *, manual_updates: bool = False) -> None:
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'results') or output.exists():
        raise ValueError('diagnostic build must be a new directory under results/')
    matches = [(path, path.read_text()) for path in (source / 'assets').glob('*.js')
               if BEFORE in path.read_text()]
    if len(matches) != 1 or matches[0][1].count(BEFORE) != 1:
        raise ValueError('unsupported player build: expected exactly one known seek implementation')
    path, original = matches[0]
    shutil.copytree(source, output)
    replacement = 'F.autoUpdate=!1,' + BEFORE if manual_updates else AFTER
    modified = original.replace(BEFORE, replacement)
    (output / path.relative_to(source)).write_text(modified)
    (output / 'heartbeat-experiment.json').write_text(json.dumps({
        'source': str(source.resolve()), 'asset': str(path.relative_to(source)),
        'original_sha256': hashlib.sha256(original.encode()).hexdigest(),
        'modified_sha256': hashlib.sha256(modified.encode()).hexdigest(),
        'experiment': ('manual camera-dependent updates; suppress redundant capture-time updates'
                       if manual_updates else 'render heartbeat while waiting for complete camera-dependent sort'),
    }, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--manual-updates', action='store_true',
                        help='disable onBeforeRender updates after first explicit seek, without heartbeat')
    args = parser.parse_args()
    prepare(args.dist.resolve(), args.output, manual_updates=args.manual_updates)
