#!/usr/bin/env python3
"""Summarize measured browser stages without confusing capture with pure render."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics


FIELDS = ('frameWallMs', 'browserSeekMs', 'browserCaptureMs', 'browserProbeMs',
          'evaluateWallMs', 'evaluateOverheadMs')


def summarize(root: Path) -> dict:
    logs = sorted(root.rglob('reference-browser.log'))
    shards = []
    for log in logs:
        frames = []
        for line in log.read_text().splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and 'frameWallMs' in value:
                frames.append(value)
        stages = {}
        for field in FIELDS:
            values = [frame[field] for frame in frames if field in frame]
            if values:
                stages[field] = {'samples': len(values), 'sum_ms': sum(values),
                                 'median_ms': statistics.median(values), 'max_ms': max(values)}
        shards.append({'log': str(log), 'frames': len(frames), 'stages': stages})
    return {'shards': shards, 'frames': sum(shard['frames'] for shard in shards),
            'note': 'Stage sums across concurrent shards are not batch wall time. Browser seek includes sort and draw; capture includes PNG/readback. Evaluate overhead includes protocol transfer and scheduling.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.root), indent=2))
