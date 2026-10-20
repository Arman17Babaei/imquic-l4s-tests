"""Historical shared20Mbps DualPI2 profile with the existing two Cubic flows.

All three streams share s1->s2. The client link remains100Mbps, and no netem
propagation is added. This transfers the historical AQM/link profile to the
current graph; it does not recreate the historical single-background topology.
Always use a fresh topology, as the player launcher does.
"""
from __future__ import annotations

import thesis_player_topology as base

ENDPOINT_ROLES = base.ENDPOINT_ROLES
METRIC_ROLES = base.METRIC_ROLES
build_topology = base.build_topology
start_background = base.start_background
assert_background = base.assert_background


def shaping_commands(device: str, role: str, mode: str, rate: str | None = None):
    commands = base.shaping_commands(device, role, mode, rate)
    if role in ENDPOINT_ROLES:
        return []
    if role == 'provider_egress':
        # Match the saved historical tc commands: burst32k, implicit cburst.
        commands[1] = ['tc', 'class', 'replace', 'dev', device, 'parent', '1:',
                       'classid', '1:1', 'htb', 'rate', '20mbit', 'burst', '32k']
    return commands


def configure_topology(devices, rate: str | None = None):
    base.configure_topology(devices, rate, command_factory=shaping_commands)
