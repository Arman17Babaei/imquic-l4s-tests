#!/usr/bin/env python3
"""Cross-check completed batch frames against separately captured baselines."""
import argparse
import json
from pathlib import Path

from benchmark_scene_batch import SCENES


def validate(batch: Path, baseline: Path) -> dict:
    results = []
    for scene, _ in SCENES:
        current = batch / scene / 'render/reference-frames'
        previous = baseline / scene / 'render/reference-frames'
        a, b = (json.loads((directory / 'frames.json').read_text()) for directory in (current, previous))
        if not a['complete'] or not b['complete'] or len(a['frames']) != len(b['frames']):
            raise ValueError(f'{scene}: incomplete comparison')
        count = 0
        for left, right in zip(a['frames'], b['frames']):
            for field in ('timestampMs', 'cameraPose', 'appliedGaussians'):
                if left[field] != right[field]:
                    raise ValueError(f'{scene}: {field} differs at {count}')
            if (current / left['file']).read_bytes() != (previous / right['file']).read_bytes():
                raise ValueError(f'{scene}: PNG differs at {count}')
            count += 1
        results.append({'scene': scene, 'frames': count, 'pngs_byte_identical': True})
    return {'complete': True, 'batch': str(batch.resolve()), 'baseline': str(baseline.resolve()),
            'scenes': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.batch, args.baseline), indent=2))
