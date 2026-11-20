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


def prepare(source: Path, output: Path, *, manual_updates: bool = False,
            synchronous_readback: bool = False, profile: bool = False,
            drain_sort: bool = False, no_read_pause: bool = False) -> None:
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'results') or output.exists():
        raise ValueError('diagnostic build must be a new directory under results/')
    matches = [(path, path.read_text()) for path in (source / 'assets').glob('*.js')
               if BEFORE in path.read_text()]
    if len(matches) != 1 or matches[0][1].count(BEFORE) != 1:
        raise ValueError('unsupported player build: expected exactly one known seek implementation')
    path, original = matches[0]
    replacement = 'F.autoUpdate=!1,' + BEFORE if manual_updates else AFTER
    if drain_sort:
        if not manual_updates:
            raise ValueError('draining requires --manual-updates')
        replacement = ('F.autoUpdate=!1,await new Promise(r=>setTimeout(r,20)),'
                       'await (async()=>{const t=performance.now();while(F.sorting){'
                       'if(performance.now()-t>30000)throw Error("sort drain timeout");'
                       'await new Promise(r=>setTimeout(r,5))}})(),' + BEFORE)
    modified = original.replace(BEFORE, replacement)
    if no_read_pause:
        if modified.count('this.readPause=1') != 1:
            raise ValueError('unsupported read pause implementation')
        modified = modified.replace('this.readPause=1', 'this.readPause=0')
    if synchronous_readback:
        before = 't.readRenderTargetPixelsAsync(e.target,0,0,Q,o,d,void 0,e.extSplats?2:1)'
        if modified.count(before) != 1:
            raise ValueError('unsupported player readback implementation')
        modified = modified.replace(before,
            'Promise.resolve(t.readRenderTargetPixels(e.target,0,0,Q,o,d,void 0,e.extSplats?2:1))')
    if profile:
        seek_before = 'await F.update({scene:j,camera:M}),N.render(j,M),N.getContext().finish(),I(),w.cameraPose'
        if modified.count(seek_before) != 1:
            raise ValueError('seek profiling requires --manual-updates')
        modified = modified.replace(seek_before,
            'await (async()=>{const t=performance.now();await F.update({scene:j,camera:M});'
            'console.log("PROFILE seek_update_ms",performance.now()-t);const r=performance.now();'
            'N.render(j,M);N.getContext().finish();console.log("PROFILE render_ms",performance.now()-r)})(),I(),w.cameraPose')
    shutil.copytree(source, output)
    (output / path.relative_to(source)).write_text(modified)
    (output / 'heartbeat-experiment.json').write_text(json.dumps({
        'source': str(source.resolve()), 'asset': str(path.relative_to(source)),
        'original_sha256': hashlib.sha256(original.encode()).hexdigest(),
        'modified_sha256': hashlib.sha256(modified.encode()).hexdigest(),
        'synchronous_readback': synchronous_readback,
        'profile': profile,
        'drain_sort': drain_sort,
        'no_read_pause': no_read_pause,
        'experiment': ('manual camera-dependent updates; suppress redundant capture-time updates'
                       if manual_updates else 'render heartbeat while waiting for complete camera-dependent sort'),
    }, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--manual-updates', action='store_true',
                        help='disable onBeforeRender updates after first explicit seek, without heartbeat')
    parser.add_argument('--synchronous-readback', action='store_true')
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--drain-sort', action='store_true')
    parser.add_argument('--no-read-pause', action='store_true')
    args = parser.parse_args()
    prepare(args.dist.resolve(), args.output, manual_updates=args.manual_updates,
            synchronous_readback=args.synchronous_readback, profile=args.profile,
            drain_sort=args.drain_sort, no_read_pause=args.no_read_pause)
