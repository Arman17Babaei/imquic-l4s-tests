#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "l4s"))

import experiment_metadata


class FakeNode:
    def __init__(self, name, ip):
        self.name = name
        self._ip = ip
        self.commands = []

    def IP(self):
        return self._ip

    def cmd(self, command):
        self.commands.append(command)
        return f"{self.name}:{command}"


class FakeSwitch:
    name = "s1"

    def intfList(self):
        return ["lo", "s1-eth1", "s1-eth2"]


class MetadataTests(unittest.TestCase):
    def test_repository_snapshot_uses_archived_source_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {
                "commit": "0123456789abcdef",
                "branch": "main",
                "dirty": False,
                "submodules": [" abc deps/example"],
            }
            (root / ".source-provenance.json").write_text(
                json.dumps(expected), encoding="utf-8"
            )
            with mock.patch.object(
                experiment_metadata, "_run",
                side_effect=AssertionError("git should not be queried"),
            ):
                self.assertEqual(
                    experiment_metadata.repository_snapshot(root), expected
                )

    def test_capture_mininet_state_uses_live_nodes(self):
        client = FakeNode("client", "10.0.0.1")
        server = FakeNode("server", "10.0.0.2")
        with mock.patch.object(experiment_metadata, "_run", side_effect=lambda args, **_: " ".join(args)):
            state = experiment_metadata.capture_mininet_state(client, server, FakeSwitch())
        self.assertEqual(state["nodes"]["client"]["ip"], "10.0.0.1")
        self.assertIn("s1-eth2", state["switch"]["interfaces"])
        self.assertTrue(any("tcp_ecn" in command for command in client.commands))
        self.assertIn("ovs-vsctl show", state["switch"]["ovs"])

    def test_write_record_is_one_file_with_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(experiment_metadata, "repository_snapshot", return_value={"commit": "abc", "submodules": []}), mock.patch.object(experiment_metadata, "system_snapshot", return_value={"kernel": "test"}):
                path = experiment_metadata.write_experiment_record(
                    output,
                    scenario="reno-step-join",
                    configuration={"bottleneck": "20mbit", "congestion": "reno"},
                    topology="client--s1--server",
                    observed_network={"switch": {"name": "s1"}},
                    argv=["runner.py", "--congestion", "reno"],
                )
            data = json.loads(path.read_text())
            self.assertEqual(data["repository"]["commit"], "abc")
            self.assertEqual(data["configuration"]["bottleneck"], "20mbit")
            self.assertEqual(data["command"]["shell"], "runner.py --congestion reno")
            self.assertEqual([p.name for p in output.iterdir()], ["provenance.json"])

    def test_detailed_tc_state_captures_qdisc_and_class(self):
        with mock.patch.object(experiment_metadata, "_run", side_effect=["qdisc state", "class state"]):
            value = experiment_metadata.detailed_tc_state("s1-eth2")
        self.assertIn("[qdisc]", value)
        self.assertIn("qdisc state", value)
        self.assertIn("[class]", value)
        self.assertIn("class state", value)

    def test_cli_key_value_configuration_for_shell_runner(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(experiment_metadata, "repository_snapshot", return_value={"commit": "abc"}), \
             mock.patch.object(experiment_metadata, "system_snapshot", return_value={"kernel": "test"}), \
             mock.patch("sys.argv", [
                 "experiment_metadata.py", "--output", directory,
                 "--scenario", "prague-timeseries",
                 "--topology", "sender--router--receiver",
                 "--set", "rate=2mbit", "--set", "target=1ms",
             ]):
            experiment_metadata.main()
            data = json.loads((Path(directory) / "provenance.json").read_text())
        self.assertEqual(data["configuration"], {"rate": "2mbit", "target": "1ms"})


if __name__ == "__main__":
    unittest.main()
