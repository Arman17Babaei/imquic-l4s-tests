"""CPU-only contracts for generated suite topology, not guest runtime evidence."""
import sys
import types
import unittest
from unittest.mock import patch

from tools.l4s.experiment_suite_topology import topology_source


def generated(switches=2, bandwidth=20.0, delay=40.0, **options):
    source = topology_source(switches, bandwidth, delay, **options)
    module = types.ModuleType("generated_suite_topology")
    exec(compile(source, "<suite-topology>", "exec"), module.__dict__)
    return module


class FakeNode:
    def __init__(self, name):
        self.name = name
        self.commands = []

    def popen(self, command, **kwargs):
        self.commands.append(command)
        return types.SimpleNamespace(returncode=0, communicate=lambda: ("", None))


class FakeNet:
    def __init__(self, **kwargs):
        self.nodes = {}
        self.links = []

    def addHost(self, name, **kwargs):
        return self._node(name)

    def addSwitch(self, name, **kwargs):
        return self._node(name)

    def _node(self, name):
        self.nodes[name] = FakeNode(name)
        return self.nodes[name]

    def addLink(self, first, second):
        self.links.append((first.name, second.name))
        number = len(self.links)
        return types.SimpleNamespace(
            intf1=types.SimpleNamespace(name=f"{first.name}-eth{number}"),
            intf2=types.SimpleNamespace(name=f"{second.name}-eth{number}"))


class SuiteTopologyTests(unittest.TestCase):
    def build(self, switches=2, bandwidth=20, delay=40, **options):
        module = generated(switches, bandwidth, delay, **options)
        modules = {
            "mininet": types.ModuleType("mininet"),
            "mininet.link": types.SimpleNamespace(TCLink=object),
            "mininet.net": types.SimpleNamespace(Mininet=FakeNet),
            "mininet.node": types.SimpleNamespace(OVSBridge=object),
        }
        with patch.dict(sys.modules, modules):
            topology = module.build_topology()
        return module, topology

    def test_background_access_delays_are_bidirectional_and_separate_from_scene(self):
        module,(net,client,server,devices)=self.build(2,50,10,background=True,
            background_link_delay_ms=50,performance_mode=True,propagation_mode='per-link')
        with patch.object(module.subprocess,'run'), patch.object(module,'configure_performance'):
            module.configure_topology(devices)
        for p in module.background_pairs():
            roles=(p['source_role'],f"s{p['index']}_to_bgsrc",
                   p['sink_role'],f"downstream_to_bgsink{p['index']}")
            for role in roles:
                node,dev=devices[role]
                redirect=next(c for c in node.commands if 'mirred' in c and dev in c)
                ifb=redirect[-1]
                delay=next(c for c in node.commands if 'netem' in c and ifb in c)
                expected='50ms'
                self.assertEqual(delay[delay.index('delay')+1],expected)
        for node in (client,server):
            delay=next(c for c in node.commands if 'netem' in c and 'ifb-delay' in c)
            self.assertEqual(delay[delay.index('delay')+1],'10ms')
        self.assertTrue(set(module.METRIC_ROLES).issubset(devices))
        for role,metric in module.main_delay_roles():
            node,dev=devices[metric]
            delay=next(c for c in node.commands if 'netem' in c and dev in c)
            self.assertEqual(delay[delay.index('delay')+1],'10ms')
        self.assertEqual(len(list(module.main_delay_roles())),6)
        self.assertEqual(module.DUALPI2_STEP,'5ms')
        cmd=module.process_command(server,'relay',['relay'])
        self.assertEqual(cmd[:6],['nice','-n','-10','taskset','-c','1'])

    def test_guest_profile_sets_prague_pacing_and_writes_parseable_evidence(self):
        import json,tempfile
        from pathlib import Path
        module=generated(performance_mode=True)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(module,'physical_roles',return_value=()), \
             patch.object(module.subprocess,'check_output',return_value=''), \
             patch.object(module,'Path') as paths, \
             patch.dict(module.os.environ,{'CAMERA_PLAYER_MODE':'prague'},clear=True):
            paths.return_value.iterdir.return_value=()
            module.set_output(Path(directory))
            module.configure_performance({})
            self.assertIn('pacing_interval=1/4000',module.os.environ['CAMERA_PLAYER_RELAY_CONGESTION_OPTIONS'])
            self.assertIn('pacing_interval=1/4000',module.os.environ['CAMERA_PLAYER_SUBSCRIBER_CONGESTION_OPTIONS'])
            evidence=json.loads((Path(directory)/'performance-profile.json').read_text())
            self.assertEqual(evidence['dualpi2_step'],'5ms')
            self.assertEqual(evidence['dualpi2_coupling_factor'],1)

    def test_source_import_does_not_require_mininet(self):
        with patch.dict(sys.modules, {"mininet": None}):
            self.assertTrue(callable(generated().build_topology))

    def test_exact_switch_chain_and_bidirectional_physical_devices(self):
        for count in (1, 2, 5):
            with self.subTest(switches=count):
                module, (net, client, server, devices) = self.build(count)
                names = ["server", *[f"s{i}" for i in range(1, count + 1)], "client"]
                self.assertEqual(net.links, list(zip(names, names[1:])))
                self.assertEqual(len(devices), 2 * (count + 1))
                self.assertEqual(set(net.nodes), set(names))
                self.assertFalse(hasattr(module, "start_background"))
                for first, second in net.links:
                    self.assertIn(f"{first}_to_{second}", devices)
                    self.assertIn(f"{second}_to_{first}", devices)

    def test_every_link_uses_aqm_and_only_client_link_is_bottleneck(self):
        module, (net, _, _, devices) = self.build(3, 25, 7.5)
        physical = dict(devices)
        with patch.object(module.subprocess, "run") as run:
            module.configure_topology(devices)
        self.assertEqual(run.call_count, 2)
        for role, (node, device) in physical.items():
            with self.subTest(role=role):
                commands = [cmd for cmd in node.commands if device in cmd]
                classes = [cmd for cmd in commands if cmd[:3] == ["tc", "class", "replace"]]
                expected = "25mbit" if "client" in role else "300mbit"
                self.assertEqual(classes[0][classes[0].index("rate") + 1], expected)
                expected_burst = "32k" if "client" in role else "512k"
                self.assertEqual(classes[0][classes[0].index("burst") + 1], expected_burst)
                self.assertEqual(classes[0][classes[0].index("cburst") + 1], expected_burst)
                aqm = [cmd for cmd in commands if "dualpi2" in cmd]
                self.assertEqual(len(aqm), 1)
                self.assertEqual(aqm[0][aqm[0].index('coupling_factor')+1],'1')
                self.assertEqual(aqm[0][-6:], ["target", "15ms", "tupdate", "16ms", "step_thresh", "5ms"])
                self.assertFalse(any("netem" in cmd for cmd in commands))

    def test_total_one_way_delay_applies_once_at_each_receiving_endpoint(self):
        module, (_, client, server, devices) = self.build(4, 20, 12.5)
        with patch.object(module.subprocess, "run"):
            module.configure_topology(devices)
        for node in (client, server):
            delays = [cmd for cmd in node.commands if "netem" in cmd]
            self.assertEqual(len(delays), 1)
            self.assertEqual(delays[0][delays[0].index("delay") + 1], "12.5ms")
            ifb = delays[0][delays[0].index("dev") + 1]
            self.assertTrue(ifb.startswith("ifb"))
            self.assertIn((node, ifb), devices.values())
            redirects = [cmd for cmd in node.commands if "mirred" in cmd]
            self.assertEqual(len(redirects), 1)
            self.assertEqual(redirects[0][-4:], ["egress", "redirect", "dev", ifb])
            self.assertFalse(any("dualpi2" in cmd and ifb in cmd for cmd in node.commands))
        self.assertTrue(set(module.METRIC_ROLES).issubset(devices))

    def test_optional_rate_override_affects_only_client_link(self):
        module, (_, _, _, devices) = self.build()
        physical = dict(devices)
        with patch.object(module.subprocess, "run"):
            module.configure_topology(devices, "55mbit")
        for role, (node, device) in physical.items():
            command = next(cmd for cmd in node.commands if device in cmd and "rate" in cmd)
            self.assertEqual(command[command.index("rate") + 1], "55mbit" if "client" in role else "300mbit")

    def test_invalid_parameters_are_rejected(self):
        for switches in (0, -1, 1.5, True, float("inf")):
            with self.subTest(switches=switches), self.assertRaises(ValueError):
                topology_source(switches, 20, 40)
        for bandwidth in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(bandwidth=bandwidth), self.assertRaises(ValueError):
                topology_source(1, bandwidth, 40)
        for delay in (-1, float("inf"), float("nan"), True):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                topology_source(1, 20, delay)
        self.assertTrue(generated(delay=0))

    def test_failed_guest_command_is_not_silently_accepted(self):
        module = generated()
        node = FakeNode("client")
        node.popen = lambda *args, **kwargs: types.SimpleNamespace(
            returncode=2, communicate=lambda: ("kernel rejected qdisc", None))
        with self.assertRaisesRegex(RuntimeError, "tc qdisc: kernel rejected qdisc"):
            module.run_command(node, ["tc", "qdisc"])

    def test_background_pairs_share_exactly_one_forward_hop(self):
        for count in (1,2,4):
            module,(net,client,server,devices)=self.build(count,50,160,background=True,background_cc='reno')
            self.assertEqual(sum(name.startswith('s') and name[1:].isdigit() for name in net.nodes),count)
            self.assertEqual(len(net.nodes),2+3*count)
            for i in range(1,count+1):
                downstream=f's{i+1}' if i<count else 'client'
                self.assertIn((f'bgsrc{i}',f's{i}'),net.links)
                self.assertIn((downstream,f'bgsink{i}'),net.links)
            self.assertEqual([p['shared_role'] for p in module.background_pairs()],
                             [f's{i}_to_s{i+1}' if i<count else f's{i}_to_client' for i in range(1,count+1)])

    def test_background_rate_cc_routing_and_single_propagation_per_direction(self):
        module,(net,client,_,devices)=self.build(2,50,160,background=True,background_cc='reno')
        with patch.object(module.subprocess,'run'):
            module.configure_topology(devices)
        cmd=module.background_command(module.background_pairs()[0])
        self.assertEqual(cmd[cmd.index('-C')+1],'reno')
        self.assertEqual(cmd[cmd.index('-b')+1],'50M')
        self.assertNotIn('-u',cmd)
        for i in (1,2):
            self.assertEqual(sum('netem' in c for c in net.nodes[f'bgsrc{i}'].commands),1)
        self.assertEqual(sum('netem' in c for c in net.nodes['bgsink1'].commands),1)
        self.assertFalse(any('netem' in c for c in net.nodes['bgsink2'].commands))
        self.assertTrue(any('net.ipv4.ip_forward=1' in c for c in client.commands))
        self.assertTrue(any('10.65.2.0/24' in c for c in net.nodes['bgsrc2'].commands))
        for node in net.nodes.values():
            for c in node.commands:
                if 'rate' in c and '300mbit' not in c:
                    self.assertIn(c[c.index('dev')+1], [devices['s2_to_client'][1],devices['client_to_s2'][1]])
        self.assertTrue(set(module.METRIC_ROLES).issubset(devices))

    def test_background_processes_are_managed_and_failure_is_logged(self):
        import tempfile,json
        from pathlib import Path
        module,(_,_,_,devices)=self.build(background=True)
        class Process:
            def __init__(self):self.pid=123;self.dead=False
            def poll(self):return 1 if self.dead else None
            def terminate(self):self.dead=True
            def wait(self,timeout=None):return 0
        spawned=[]
        def popen(command,**kwargs):
            p=Process();spawned.append(p);return p
        for node,_ in devices.values():node.popen=popen
        with tempfile.TemporaryDirectory() as tmp, patch.object(module.time,'sleep') as sleep, \
             patch.object(module,'received_background_data',return_value=True), \
             patch.object(module,'assert_background',return_value={'5201':'established','5202':'established'}):
            result=module.start_background(devices,Path(tmp))
            self.assertEqual(len(result),4)
            self.assertEqual(json.loads((Path(tmp)/'background-config.json').read_text())['flow_count'],2)
            self.assertEqual(json.loads((Path(tmp)/'background-config.json').read_text())['warmup_seconds'],4)
            self.assertEqual(sleep.call_args.args,(4,))
            for p,h in result:p.terminate();h.close()
        spawned.clear()
        with tempfile.TemporaryDirectory() as tmp, patch.object(module.time,'sleep'), \
             patch.object(module.time,'monotonic',side_effect=[0,100]), \
             patch.object(module,'assert_background',side_effect=RuntimeError('no TCP connection')):
            with self.assertRaisesRegex(RuntimeError,'no TCP connection'):
                module.start_background(devices,Path(tmp))
            self.assertTrue(all(p.dead for p in spawned))
            self.assertIn('no TCP connection',(Path(tmp)/'background-failure.txt').read_text())

    def test_control_connection_without_data_is_not_background_readiness(self):
        module,(_,_,_,devices)=self.build(1,background=True)
        state='0 0 10.64.1.1:30000 10.65.1.2:5201\n cubic bytes_sent:200\n'
        with patch.object(module,'run_command',return_value=state):
            with self.assertRaisesRegex(RuntimeError,'data connection'):
                module.assert_background(devices)
        with patch.object(module,'run_command',return_value=state+state.replace('30000','30001')):
            self.assertTrue(module.assert_background(devices))

    def test_background_readiness_requires_nonzero_receiver_payload(self):
        import tempfile
        from pathlib import Path
        module=generated(background=True)
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/'sink.log'
            log.write_text('Accepted connection from 10.64.1.1, port 30000\n')
            self.assertFalse(module.received_background_data(log))
            log.write_text('[  5]   0.00-0.10 sec  0.00 Bytes  0.00 bits/sec\n')
            self.assertFalse(module.received_background_data(log))
            log.write_text('[  5]   0.00-0.10 sec  128 KBytes  10.5 Mbits/sec\n')
            self.assertTrue(module.received_background_data(log))


if __name__ == "__main__":
    unittest.main()
