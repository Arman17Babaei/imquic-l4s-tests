"""CPU regression coverage for the thesis network contract."""
import importlib.util
import sys
import unittest
from unittest.mock import patch
from types import ModuleType, SimpleNamespace
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / 'tools' / 'l4s'
sys.path.insert(0, str(TOOLS))


class ThesisTopologyTests(unittest.TestCase):
    def module(self):
        path = TOOLS / 'thesis_player_topology.py'
        self.assertTrue(path.exists(), 'thesis topology module must exist')
        spec = importlib.util.spec_from_file_location('thesis_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_same_dualpi2_profile_for_both_controllers(self):
        module = self.module()
        for mode in ('reno', 'prague'):
            provider = module.shaping_commands('s1-eth2', 'provider_egress', mode)
            downstream = module.shaping_commands('s2-eth2', 'downstream_egress', mode)
            self.assertIn('300mbit', provider[1])
            self.assertIn('512k', provider[1])
            self.assertIn('100mbit', downstream[1])
            self.assertIn('128k', downstream[1])
            self.assertIn('dualpi2', provider[2])
            for commands in (provider, downstream):
                self.assertIn('dualpi2', commands[2])
                self.assertEqual(commands[2][-6:], ['target', '15ms', 'tupdate', '16ms', 'step_thresh', '1ms'])
            self.assertEqual(module.shaping_commands('s2-eth2', 'downstream_egress', 'reno'),
                             module.shaping_commands('s2-eth2', 'downstream_egress', 'prague'))
            self.assertFalse(any('netem' in command for command in provider + downstream))

    def test_default_player_restores_one_ms_step_without_changing_classic_pi(self):
        import player_topology
        commands=player_topology.shaping_commands('s1-eth1',propagation=False)
        self.assertEqual(commands[-1][-6:], ['target','15ms','tupdate','16ms','step_thresh','1ms'])

    def test_endpoint_delay_and_background_command(self):
        module = self.module()
        for role in module.ENDPOINT_ROLES:
            commands = module.shaping_commands('test-eth0', role, 'reno')
            self.assertEqual(len(commands), 1)
            self.assertIn('10ms', commands[0])
        for port in (5201, 5202):
            command = module.background_command('10.0.0.3', port)
            self.assertIn('cubic', command)
            self.assertNotIn('-b', command)
            self.assertEqual(command[command.index('-p') + 1], str(port))
        with self.assertRaises(ValueError): module.shaping_commands('x', 'provider_egress', 'cubic')

    def test_queue_parser_retains_marks_and_both_delays(self):
        from run_camera_player_guest import parse_qdisc_stats
        result = parse_qdisc_stats('qdisc dualpi2 10: parent 1:1\n Sent 300 bytes 4 pkt (dropped 2, overlimits 7 requeues 0)\n backlog 1234b 12p requeues 0\n  delay_c 15000us delay_l 400us\n  ecn_mark 44\n')
        self.assertEqual(result['backlog_bytes'], 1234)
        self.assertEqual(result['backlog_packets'], 12)
        self.assertEqual(result['drops'], 2)
        self.assertEqual(result['ecn_mark'], 44)
        self.assertEqual(result['delay_c_us'], 15000)
        self.assertEqual(result['delay_l_us'], 400)

    def test_exact_graph_and_interface_ownership(self):
        module = self.module()
        class Network:
            def __init__(self, **kwargs):
                self.nodes = {}
                self.links = []
                self.ports = {}
            def addHost(self, name, **kwargs):
                self.nodes[name] = SimpleNamespace(name=name, **kwargs)
                return self.nodes[name]
            def addSwitch(self, name, **kwargs):
                return self.addHost(name, **kwargs)
            def addLink(self, first, second):
                self.links.append((first.name, second.name))
                interfaces = []
                for node in (first, second):
                    start = 1 if node.name.startswith('s') and node.name != 'server' else 0
                    port = self.ports.get(node.name, start)
                    self.ports[node.name] = port + 1
                    interfaces.append(SimpleNamespace(name=f'{node.name}-eth{port}'))
                return SimpleNamespace(intf1=interfaces[0], intf2=interfaces[1])
        modules = {name: ModuleType(name) for name in ('mininet', 'mininet.link', 'mininet.net', 'mininet.node')}
        modules['mininet.net'].Mininet = Network
        modules['mininet.link'].TCLink = object
        modules['mininet.node'].OVSBridge = object
        with patch.dict(sys.modules, modules):
            net, client, server, devices = module.build_topology()
        self.assertEqual(server.ip, '10.0.0.1/24')
        self.assertEqual(client.ip, '10.0.0.2/24')
        self.assertEqual(net.links, [('server', 's1'), ('s1', 's2'), ('s2', 'client'),
                                     ('s2', 'bg_sink'), ('bg_src', 's1'), ('bg_sink2', 's2')])
        self.assertEqual(devices['provider_egress'][1], 's1-eth2')
        self.assertEqual(devices['downstream_egress'][1], 's2-eth2')

    def test_rate_override_rejected_before_any_network_change(self):
        module = self.module()
        for rate in ('10mbit', '20Mbps', '300mbit', ''):
            with self.assertRaisesRegex(ValueError, '20mbit or 100mbit'):
                module.configure_topology({}, rate)

    def test_twenty_mbps_only_changes_downstream_rate_preserving_bursts(self):
        module = self.module()
        for mode in ('reno', 'prague'):
            default = module.shaping_commands('s2-eth2', 'downstream_egress', mode)
            explicit = module.shaping_commands('s2-eth2', 'downstream_egress', mode, '100mbit')
            low = module.shaping_commands('s2-eth2', 'downstream_egress', mode, '20mbit')
            self.assertEqual(default, explicit)
            expected = [[value.replace('100mbit', '20mbit') for value in command] for command in default]
            self.assertEqual(low, expected)
            self.assertEqual(module.shaping_commands('s1-eth2', 'provider_egress', mode, '20mbit'),
                             module.shaping_commands('s1-eth2', 'provider_egress', mode))


if __name__ == '__main__': unittest.main()
