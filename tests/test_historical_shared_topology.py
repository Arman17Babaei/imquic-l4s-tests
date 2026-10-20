"""Guard the restored shared bottleneck without changing background traffic."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/l4s'))


class HistoricalSharedTopologyTest(unittest.TestCase):
    def module(self):
        import historical_shared_topology
        return historical_shared_topology

    def test_identical_queues_and_historical_shared_rate_for_both_senders(self):
        module = self.module()
        for mode in ('reno', 'prague'):
            provider = module.shaping_commands('s1-eth2', 'provider_egress', mode)
            downstream = module.shaping_commands('s2-eth2', 'downstream_egress', mode)
            self.assertIn('20mbit', provider[1])
            self.assertIn('32k', provider[1])
            self.assertNotIn('cburst', provider[1])
            self.assertIn('100mbit', downstream[1])
            self.assertIn('128k', downstream[1])
            for commands in (provider, downstream):
                self.assertEqual(commands[-1][-7:], ['dualpi2', 'target', '15ms',
                                                    'tupdate', '16ms', 'step_thresh', '1ms'])
        self.assertEqual(module.shaping_commands('s1-eth2', 'provider_egress', 'reno'),
                         module.shaping_commands('s1-eth2', 'provider_egress', 'prague'))

    def test_no_added_propagation_and_background_is_unchanged(self):
        module = self.module()
        import thesis_player_topology as base
        self.assertIs(module.build_topology, base.build_topology)
        self.assertIs(module.start_background, base.start_background)
        self.assertIs(module.assert_background, base.assert_background)
        for role in module.ENDPOINT_ROLES:
            self.assertEqual(module.shaping_commands('eth0', role, 'reno'), [])
        self.assertIn('300mbit', base.shaping_commands('s1-eth2', 'provider_egress', 'reno')[1])

    def test_configuration_injects_commands_without_mutating_base(self):
        module = self.module()
        import thesis_player_topology as base
        original = base.shaping_commands
        with patch.object(base, 'configure_topology') as configure:
            module.configure_topology({}, '100mbit')
        configure.assert_called_once_with({}, '100mbit', command_factory=module.shaping_commands)
        self.assertIs(base.shaping_commands, original)

    def test_actual_configurator_uses_shared_profile_in_both_modes(self):
        module = self.module()
        import thesis_player_topology as base
        for mode in ('reno', 'prague'):
            roles = ('provider_egress', 'downstream_egress', *module.ENDPOINT_ROLES)
            nodes = {role: Mock() for role in roles}
            for node in nodes.values():
                node.popen.return_value.returncode = 0
                node.popen.return_value.communicate.return_value = ('', None)
            devices = {role: (node, role + '-eth0') for role, node in nodes.items()}
            with patch.dict(os.environ, {'CAMERA_PLAYER_MODE': mode}), patch.object(base.subprocess, 'run'):
                module.configure_topology(devices)
            commands = {role: [call.args[0] for call in node.popen.call_args_list]
                        for role, node in nodes.items()}
            self.assertTrue(any('20mbit' in command for command in commands['provider_egress']))
            self.assertTrue(any('100mbit' in command for command in commands['downstream_egress']))
            self.assertFalse(any('netem' in command for command in commands['server_egress']))


if __name__ == '__main__':
    unittest.main()
