#!/usr/bin/env python3
"""Small provenance helpers for the IMQUIC experiment runners."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str], *, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout.strip()
    except OSError as error:
        return f"unavailable: {error}"


def _first_line(value: str) -> str:
    return value.splitlines()[0] if value.splitlines() else ""


def _memory_total_kib() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except (OSError, IndexError):
        pass
    return platform.processor()


def repository_snapshot(root: Path = ROOT) -> dict[str, Any]:
    archived_snapshot = root / ".source-provenance.json"
    if archived_snapshot.is_file():
        try:
            snapshot = json.loads(archived_snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return {"error": f"invalid archived repository snapshot: {error}"}
        if isinstance(snapshot, dict):
            return snapshot
        return {"error": "invalid archived repository snapshot: expected object"}
    status = _run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root)
    return {
        "commit": _run(["git", "rev-parse", "HEAD"], cwd=root),
        "branch": _run(["git", "branch", "--show-current"], cwd=root),
        "dirty": bool(status),
        "status": status.splitlines(),
        "submodules": _run(["git", "submodule", "status", "--recursive"], cwd=root).splitlines(),
        "remotes": _run(["git", "remote", "-v"], cwd=root).splitlines(),
        "submodule_urls": _run([
            "git", "config", "--file", ".gitmodules", "--get-regexp",
            r"^submodule\..*\.url$",
        ], cwd=root).splitlines(),
    }


def system_snapshot() -> dict[str, Any]:
    tools = {
        "python": sys.version.splitlines()[0],
        "mininet": _run(["mn", "--version"]),
        "openvswitch": _run(["ovs-vsctl", "--version"]),
        "tc": _run(["tc", "-V"]),
        "iperf3": _first_line(_run(["iperf3", "--version"])),
        "tcpdump": _first_line(_run(["tcpdump", "--version"])),
        "tshark": _first_line(_run(["tshark", "--version"])),
    }
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "uname": " ".join(platform.uname()),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "memory_total_kib": _memory_total_kib(),
        "os_release": _run(["cat", "/etc/os-release"]),
        "tools": tools,
        "dualpi2_modinfo": _run(["modinfo", "sch_dualpi2"]),
    }


def _node_state(node: Any) -> dict[str, str]:
    commands = {
        "addresses": "ip -details addr show",
        "routes": "ip route show table all",
        "tcp_ecn": "sysctl -n net.ipv4.tcp_ecn 2>/dev/null || true",
        "congestion_available": (
            "sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true"
        ),
        "congestion_default": (
            "sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || true"
        ),
        "mangle_output": "iptables -t mangle -S OUTPUT 2>/dev/null || true",
    }
    return {name: node.cmd(command).strip() for name, command in commands.items()}


def capture_mininet_state(client: Any, server: Any, switch: Any) -> dict[str, Any]:
    """Capture enough live state to verify the intended topology was applied."""
    interfaces = [str(interface) for interface in switch.intfList()]
    return {
        "nodes": {
            "client": {"ip": client.IP(), **_node_state(client)},
            "server": {"ip": server.IP(), **_node_state(server)},
        },
        "switch": {
            "name": switch.name,
            "interfaces": interfaces,
            "ovs": _run(["ovs-vsctl", "show"]),
            "links": _run(["ip", "-details", "link", "show"]),
        },
    }


def write_experiment_record(
    output: Path,
    *,
    scenario: str,
    configuration: dict[str, Any],
    topology: dict[str, Any] | str,
    observed_network: dict[str, Any] | None = None,
    argv: Iterable[str] | None = None,
) -> Path:
    """Write one human-inspectable JSON provenance file at the run root."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    command_argv = list(sys.argv if argv is None else argv)
    record = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scenario": scenario,
        "command": {
            "argv": command_argv,
            "shell": shlex.join(command_argv),
            "cwd": str(Path.cwd()),
        },
        "repository": repository_snapshot(),
        "system": system_snapshot(),
        "configuration": configuration,
        "topology": topology,
        "observed_network": observed_network or {},
    }
    destination = output / "provenance.json"
    destination.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def detailed_tc_state(interface: str) -> str:
    """Return qdisc and class state together for a bottleneck interface."""
    sections = []
    for kind in ("qdisc", "class"):
        sections.append(f"[{kind}]\n")
        sections.append(_run(["tc", "-s", "-d", kind, "show", "dev", interface]))
        sections.append("\n")
    return "".join(sections)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--topology", required=True)
    parser.add_argument("--set", action="append", default=[], dest="settings")
    args = parser.parse_args()
    configuration = {}
    for assignment in args.settings:
        if "=" not in assignment:
            parser.error(f"--set requires key=value, got {assignment!r}")
        key, value = assignment.split("=", 1)
        configuration[key] = value
    write_experiment_record(
        args.output,
        scenario=args.scenario,
        configuration=configuration,
        topology=args.topology,
    )


if __name__ == "__main__":
    main()
