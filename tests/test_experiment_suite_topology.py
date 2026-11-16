"""CPU-only contracts for generated suite topology, not guest runtime evidence."""
import sys
import types
import unittest
from unittest.mock import patch

from tools.l4s.experiment_suite_topology import topology_source


def generated(bandwidth=20.0, delay=40.0, **options):
    source = topology_source(bandwidth, delay, **options)
    module = types.ModuleType("generated_suite_topology")
    exec(compile(source, "<suite-topology>", "exec"), module.__dict__)
    return module


class FakeNode:
    def __init__(self, name):
        self.name = name
        self.commands = []

    def popen(self, command, **kwargs):
        self.commands.append(command)
        return types.SimpleNamespace(returncode=0, communicate=lambda **kw: ("", None))


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
    def build(self, **options):
        module = generated(**options)
        modules = {
            "mininet": types.ModuleType("mininet"),
            "mininet.link": types.SimpleNamespace(TCLink=object),
            "mininet.net": types.SimpleNamespace(Mininet=FakeNet),
            "mininet.node": types.SimpleNamespace(OVSBridge=object),
        }
        with patch.dict(sys.modules, modules):
            topology = module.build_topology()
        return module, topology

    def test_exactly_two_switches_and_three_pairs_share_the_inter_switch_link(self):
        module, (net, client, server, devices) = self.build()
        self.assertEqual(set(net.nodes), {'s1','s2','server','client',
                                         'cubicsrc','cubicsink','praguesrc','praguesink'})
        self.assertEqual(len(net.links), 7)
        self.assertIn(('s1','s2'), net.links)
        self.assertIn(('server','s1'), net.links)
        self.assertIn(('s2','client'), net.links)
        for pair in module.background_pairs():
            self.assertIn((pair['source'],'s1'), net.links)
            self.assertIn(('s2',pair['sink']), net.links)
            self.assertEqual(pair['shared_role'],'s1_to_s2')
        self.assertEqual(len(devices), 14)
        self.assertTrue(set(module.METRIC_ROLES).issubset(devices))

    def test_host_names_fit_linux_interface_name_limit(self):
        _,(net,_,_,_)=self.build()
        for name in net.nodes:
            self.assertLessEqual(len(name+'-eth0'),15,name)

    def test_shared_bottleneck_has_configurable_aqm_and_separate_propagation(self):
        module, (_, _, _, devices) = self.build(bandwidth=25, delay=12,
            dualpi2_target_ms=10, dualpi2_update_ms=20, dualpi2_step_ms=2,
            dualpi2_coupling_factor=2)
        with patch.object(module.subprocess,'run'):
            module.configure_topology(devices, '55mbit')
        for role, (node, dev) in devices.items():
            commands=[c for c in node.commands if dev in c]
            rate=next(c for c in commands if c[:3]==['tc','class','replace'])
            shared=role in ('s1_to_s2','s2_to_s1')
            self.assertEqual(rate[rate.index('rate')+1], '55mbit' if shared else '300mbit')
            aqm=[c for c in commands if 'dualpi2' in c]
            delays=[c for c in commands if 'netem' in c]
            if shared:
                self.assertEqual(len(aqm),1)
                self.assertEqual(aqm[0][-8:], ['coupling_factor','2','target','10ms','tupdate','20ms','step_thresh','2ms'])
                self.assertEqual(delays,[])
            else:
                self.assertEqual(aqm,[])
                self.assertEqual(len(delays),1)
                self.assertEqual(delays[0][delays[0].index('delay')+1],'6ms')

    def test_backgrounds_are_one_unlimited_cubic_and_one_unlimited_prague(self):
        module=generated()
        self.assertEqual([p['cc'] for p in module.background_pairs()], ['cubic','prague'])
        for pair in module.background_pairs():
            command=module.background_command(pair)
            self.assertEqual(command[command.index('-C')+1],pair['cc'])
            for forbidden in ('-b','--bitrate','-P','--parallel','-u'):
                self.assertNotIn(forbidden,command)
        self.assertEqual(module.DUALPI2_STEP_MS,1)

    def test_ecn_is_enabled_only_for_prague_endpoints_and_algorithms_are_explicit(self):
        module,(_,_,_,devices)=self.build()
        with patch.object(module.subprocess,'run') as run:
            module.configure_topology(devices)
        self.assertIn(unittest.mock.call(['modprobe','tcp_prague'],check=True),run.call_args_list)
        for pair in module.background_pairs():
            for role in (pair['source_role'],pair['sink_role']):
                node,_=devices[role]
                self.assertIn(['sysctl','-w',f"net.ipv4.tcp_congestion_control={pair['cc']}"],node.commands)
                self.assertIn(['sysctl','-w',f"net.ipv4.tcp_ecn={3 if pair['cc']=='prague' else 0}"],node.commands)

    def test_invalid_parameters_fail_before_source_is_generated(self):
        for name in ('bandwidth','delay','dualpi2_target_ms','dualpi2_update_ms',
                     'dualpi2_step_ms','dualpi2_coupling_factor','background_warmup_seconds'):
            for value in (True, -1, float('nan'), float('inf')):
                with self.subTest(name=name,value=value), self.assertRaises(ValueError):
                    generated(**{name:value})
        for name in ('bandwidth','dualpi2_target_ms','dualpi2_update_ms','dualpi2_coupling_factor'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                generated(**{name:0})
        generated(delay=0,dualpi2_step_ms=0,background_warmup_seconds=0)

    def test_control_socket_and_prague_without_ecn_are_rejected(self):
        module,(_,_,_,devices)=self.build()
        def state(pair, ecn=True):
            return ''.join(f"0 0 10.0.0.3:{port} {pair['destination']}:{pair['port']}\n "
                           f"{pair['cc']} {'ecn' if ecn and pair['cc']=='prague' else ''} bytes_sent:20000\n"
                           for port in (30000,30001))
        def command(node, cmd):
            pair=next(p for p in module.background_pairs() if str(p['port'])==cmd[-1])
            return state(pair)
        with patch.object(module,'run_command',side_effect=command):
            self.assertEqual(len(module.assert_background(devices)),2)
        with patch.object(module,'run_command',return_value='0 0 10.0.0.3:30000 10.0.0.4:5201\n cubic\n'):
            with self.assertRaisesRegex(RuntimeError,'data connection'):
                module.assert_background(devices)
        def no_ecn(node,cmd):
            pair=next(p for p in module.background_pairs() if str(p['port'])==cmd[-1])
            return state(pair,ecn=False)
        with patch.object(module,'run_command',side_effect=no_ecn):
            with self.assertRaisesRegex(RuntimeError,'ECN'):
                module.assert_background(devices)
        def fallback(node,cmd):
            return command(node,cmd).replace('prague','cubic')
        with patch.object(module,'run_command',side_effect=fallback):
            with self.assertRaisesRegex(RuntimeError,'prague'):
                module.assert_background(devices)

    def test_older_ss_uses_actual_ce_feedback_as_ecn_evidence(self):
        module,(_,_,_,devices)=self.build()
        def state(node,cmd):
            pair=next(p for p in module.background_pairs() if str(p['port'])==cmd[-1])
            return ''.join(f"0 0 10.0.0.3:{port} {pair['destination']}:{pair['port']}\n "
                           f"{pair['cc']} bytes_sent:{sent} delivered_ce:{ce}\n"
                           for port,sent,ce in ((30000,166,0),(30001,200000,123)))
        with patch.object(module,'run_command',side_effect=state):
            self.assertEqual(len(module.assert_background(devices)),2)
        def no_ce(node,cmd):return state(node,cmd).replace('delivered_ce:123','delivered_ce:0')
        with patch.object(module,'run_command',side_effect=no_ce):
            with self.assertRaisesRegex(RuntimeError,'ECN'):module.assert_background(devices)

    def test_background_readiness_requires_receiver_payload(self):
        import tempfile
        from pathlib import Path
        module=generated()
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/'sink.log'
            for text in ('Accepted connection', '[ 5] 0.00-0.10 sec 0.00 Bytes 0.00 bits/sec'):
                log.write_text(text)
                self.assertFalse(module.received_background_data(log))
            log.write_text('[ 5] 0.00-0.10 sec 128 KBytes 10.5 Mbits/sec')
            self.assertTrue(module.received_background_data(log))

    def test_startup_orders_receivers_then_senders_and_cleans_up_on_failure(self):
        import json,tempfile
        from pathlib import Path
        module,(_,_,_,devices)=self.build(background_warmup_seconds=6)
        spawned=[]
        class Process:
            pid=123
            dead=False
            def poll(self):return 1 if self.dead else None
            def terminate(self):self.dead=True
            def wait(self,timeout=None):return 0
        def popen(command,**kw):
            process=Process();spawned.append((process,command));return process
        for node,_ in devices.values():node.popen=popen
        with tempfile.TemporaryDirectory() as tmp, patch.object(module.time,'sleep') as sleep, \
             patch.object(module,'received_background_data',return_value=True), \
             patch.object(module,'assert_background',return_value={'5201':'ok','5202':'ok'}):
            result=module.start_background(devices,Path(tmp))
            self.assertEqual(len(result),4)
            self.assertTrue(all('-s' in c for p,c in spawned[:2]))
            self.assertTrue(all('-c' in c for p,c in spawned[2:]))
            self.assertEqual(sleep.call_args.args,(6,))
            config=json.loads((Path(tmp)/'background-config.json').read_text())
            self.assertEqual(config['status'],'PASS')
            self.assertEqual(config['rate'],'unlimited')
            self.assertEqual([p['cc'] for p in config['pairs']],['cubic','prague'])
            self.assertEqual([p['tcp_ecn'] for p in config['pairs']],[0,3])
            for p,h in result:p.terminate();h.close()
        spawned.clear()
        with tempfile.TemporaryDirectory() as tmp, patch.object(module.time,'sleep'), \
             patch.object(module.time,'monotonic',side_effect=[0,100]), \
             patch.object(module,'assert_background',side_effect=RuntimeError('no TCP data connection')):
            with self.assertRaisesRegex(RuntimeError,'data connection'):
                module.start_background(devices,Path(tmp))
            self.assertTrue(all(p.dead for p,c in spawned))
            self.assertEqual(json.loads((Path(tmp)/'background-config.json').read_text())['status'],'FAIL')

    def test_performance_profile_records_aqm_and_preserves_explicit_pacing(self):
        import json,tempfile
        from pathlib import Path
        module=generated(performance_mode=True,dualpi2_step_ms=2)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(module.subprocess,'check_output',return_value=''), \
             patch.object(module,'Path') as paths, \
             patch.dict(module.os.environ,{'CAMERA_PLAYER_MODE':'prague',
                 'CAMERA_PLAYER_RELAY_CONGESTION_OPTIONS':'explicit-pacing'},clear=True):
            paths.return_value.iterdir.return_value=()
            module.set_output(Path(tmp))
            module.configure_performance()
            self.assertEqual(module.os.environ['CAMERA_PLAYER_RELAY_CONGESTION_OPTIONS'],'explicit-pacing')
            self.assertIn('pacing_interval=1/4000',module.os.environ['CAMERA_PLAYER_SUBSCRIBER_CONGESTION_OPTIONS'])
            profile=json.loads((Path(tmp)/'performance-profile.json').read_text())
            self.assertEqual(profile['dualpi2_step_ms'],2)
            self.assertEqual(profile['dualpi2_coupling_factor'],1)

    def test_source_is_standalone_and_guest_command_failure_surfaces(self):
        module=generated(performance_mode=True)
        self.assertTrue(callable(module.build_topology))
        node=FakeNode('client')
        node.popen=lambda *a,**kw: types.SimpleNamespace(returncode=2,communicate=lambda **kw: ('kernel rejected qdisc',None))
        with self.assertRaisesRegex(RuntimeError,'kernel rejected qdisc'):
            module.run_command(node,['tc','qdisc'])
        self.assertIn('taskset',module.process_command(node,'relay',['relay']))
