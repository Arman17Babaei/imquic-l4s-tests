"""Emit standalone configured copies of the maintained player topology."""
from __future__ import annotations

import math
from pathlib import Path


def topology_source(bandwidth_mbps: float, delay_ms: float, *,
                    dualpi2_target_ms: float = 15, dualpi2_update_ms: float = 16,
                    dualpi2_step_ms: float = 1, dualpi2_coupling_factor: int = 3,
                    background_warmup_seconds: float = 4, performance_mode: bool = False) -> str:
    settings = dict(BOTTLENECK=f'{bandwidth_mbps:g}mbit', PROPAGATION_DELAY_MS=delay_ms,
                    DUALPI2_TARGET_MS=dualpi2_target_ms, DUALPI2_UPDATE_MS=dualpi2_update_ms,
                    DUALPI2_STEP_MS=dualpi2_step_ms, DUALPI2_COUPLING_FACTOR=dualpi2_coupling_factor,
                    BACKGROUND_WARMUP_SECONDS=background_warmup_seconds, PERFORMANCE_MODE=performance_mode)
    for name, value in dict(bandwidth_mbps=bandwidth_mbps, delay_ms=delay_ms,
                            **{k:v for k,v in settings.items() if k not in ('BOTTLENECK','PERFORMANCE_MODE')}).items():
        allow_zero = name in ('delay_ms','PROPAGATION_DELAY_MS','DUALPI2_STEP_MS','BACKGROUND_WARMUP_SECONDS')
        if (isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value)
                or value < 0 or (value == 0 and not allow_zero)):
            raise ValueError(f'invalid {name}: must be finite and {"nonnegative" if allow_zero else "positive"}')
    if not isinstance(performance_mode, bool):
        raise ValueError('performance_mode must be boolean')
    if isinstance(dualpi2_coupling_factor, bool) or not isinstance(dualpi2_coupling_factor, int):
        raise ValueError('dualpi2_coupling_factor must be an integer')
    if dualpi2_coupling_factor > 255:
        raise ValueError('dualpi2_coupling_factor must be <=255')
    source = Path(__file__).with_name('player_topology.py').read_text()
    return source + '\n# Experiment configuration overrides.\n' + ''.join(
        f'{name} = {value!r}\n' for name, value in settings.items())
