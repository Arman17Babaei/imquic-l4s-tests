"""Editable Mininet topology for player runs inside QEMU.

Edit build_topology and configure_topology directly for a different graph or
queue layout. The player only needs the client/server nodes and the interfaces
returned for before/after evidence. Imports work on the host without Mininet.
"""
from __future__ import annotations

import subprocess

BOTTLENECK = "20mbit"
PROPAGATION_DELAY = "40ms"  # Each direction crosses two shaped egresses: nominal RTT 80 ms.
DUALPI2_TARGET = "15ms"
DUALPI2_UPDATE = "16ms"
DUALPI2_STEP = "1ms"


def build_topology():
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSBridge

    net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
    client = net.addHost("client", ip="10.0.0.1/24")
    switch = net.addSwitch("s1", failMode="standalone")
    server = net.addHost("server", ip="10.0.0.2/24")
    first = net.addLink(client, switch)
    second = net.addLink(switch, server)
    devices = {
        "client_to_s1": (client, first.intf1.name),
        "s1_to_client": (switch, first.intf2.name),
        "s1_to_server": (switch, second.intf1.name),
        "server_to_s1": (server, second.intf2.name),
    }
    return net, client, server, devices


def shaping_commands(device: str, *, propagation: bool, rate: str | None = None):
    # Keep netem on a separate egress from the dropping AQM; parenting DualPI2
    # below netem has caused accounting underflow on the experiment kernel.
    if propagation:
        return [["tc", "qdisc", "replace", "dev", device, "root", "handle", "1:",
                 "netem", "delay", PROPAGATION_DELAY, "limit", "100000"]]
    return [
        ["tc", "qdisc", "replace", "dev", device, "root", "handle", "10:", "htb", "default", "1"],
        ["tc", "class", "replace", "dev", device, "parent", "10:", "classid", "10:1",
         "htb", "rate", rate or BOTTLENECK, "burst", "32k", "cburst", "32k"],
        ["tc", "qdisc", "replace", "dev", device, "parent", "10:1", "handle", "20:",
         "dualpi2", "target", DUALPI2_TARGET, "tupdate", DUALPI2_UPDATE, "step_thresh", DUALPI2_STEP],
    ]


def configure_topology(devices, rate: str | None = None):
    subprocess.run(["modprobe", "sch_dualpi2"], check=True)
    for name, (node, device) in devices.items():
        for command in shaping_commands(device, propagation=name.startswith("s1_to_"), rate=rate):
            if node is None:
                subprocess.run(command, check=True)
            else:
                process = node.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                output, _ = process.communicate()
                if process.returncode:
                    raise RuntimeError(f"{' '.join(command)}: {output}")
