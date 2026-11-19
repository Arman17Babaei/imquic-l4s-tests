from __future__ import annotations
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools/l4s'
sys.path.insert(0, str(TOOLS))


class ExperimentSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'results').mkdir()
        (self.root/'manifest.json').write_text('{}')
        self.source = [{'timestamp_ms': 0, 'camera_position': [1,2,3]},
                       {'timestamp_ms': 10000, 'camera_position': [4,5,6]},
                       {'timestamp_ms': 19000, 'camera_position': [7,8,9]}]
        (self.root/'trace.json').write_text(json.dumps(self.source))
        self.config_file = self.root/'suite.toml'
        self.config_file.write_text('''version = 1
output = "results/batch"
[runner]
memory_limit_mib = 0
min_available_memory_gib = 0
min_free_disk_gib = 0
[analysis]
enabled = false
[scenes.fixture]
manifest = "manifest.json"
trace = "trace.json"
[defaults]
scenes = ["fixture"]
bandwidth_mbps = 50
delays_ms = [160, 320]
duration_seconds = 5
path = "ideal"
reruns = 2
on_failure = "continue"
[[experiments]]
name = "example"
''')
        self.config = importlib.import_module('experiment_suite_config')
        self.runner = importlib.import_module('run_experiments')

    def load(self):
        return self.config.load_config(self.config_file, repo_root=self.root)

    def test_expansion_and_ideal_commands(self):
        cfg = self.load(); cases = self.config.expand_cases(cfg)
        self.assertEqual(len(cases), 2)
        self.assertEqual({c['delay_ms'] for c in cases}, {160,320})
        self.assertEqual({c['rerun'] for c in cases}, {1})
        self.assertEqual({c['delivery_model'] for c in cases}, {'offline-imquic-v1'})
        self.assertEqual({c['controller'] for c in cases}, {'ideal'})
        self.assertEqual({c['switches'] for c in cases}, {0})
        command = self.runner.build_command(cases[0], cfg, self.root/'results/out', self.root/'scaled.json')
        self.assertEqual(command[command.index('--reference-cache')+1], str(Path(cfg['output'])/'references'))
        self.assertIn('--ideal-bandwidth-mbps', command)
        self.assertNotIn('--qemu-base', command)
        self.assertEqual(command[command.index('--quality-camera-timestamps')+1], 'fixed')
        self.assertNotIn('--start-after-first-object',command)
        self.assertEqual(command[command.index('--max-missing-snapshot-fraction')+1],'0')

    def test_ideal_runs_higher_delay_then_lower_for_each_scene_and_repeat(self):
        cfg=self.load()
        cfg['experiments'][0].update(delays_ms=[160,80,320],reruns=2)
        cases=self.config.expand_cases(cfg)
        self.assertEqual([(c['rerun'],c['delay_ms']) for c in cases],
                         [(1,320),(1,160),(1,80)])

    def test_qemu_keeps_prague_reno_adjacent_at_same_delay_across_repeats(self):
        cfg=self.load()
        cfg['experiments'][0].update(path='qemu',controllers=['reno','prague'],reruns=2)
        cases=self.config.expand_cases(cfg)
        self.assertEqual([(c['rerun'],c['delay_ms'],c['controller']) for c in cases],
                         [(r,d,c) for r in (1,2) for d in (160,320) for c in ('prague','reno')])

    def test_reno_ect0_expands_as_distinct_qemu_case_and_reaches_runner(self):
        self.config_file.write_text(self.config_file.read_text().replace(
            'path = "ideal"', 'path = "qemu"\ncontrollers = ["reno-ect0", "reno", "prague"]'))
        cfg=self.load()
        cases=self.config.expand_cases(cfg)
        self.assertEqual([c['controller'] for c in cases[:3]], ['prague','reno','reno-ect0'])
        self.assertEqual(len(cases),12)
        self.assertEqual(len({c['id'] for c in cases}),12)
        case=next(c for c in cases if c['controller']=='reno-ect0')
        command=self.runner.build_command(case,cfg,self.root/'results/out',self.root/'scaled.json',self.root/'topology.py')
        self.assertEqual(command[command.index('--transport-mode')+1],'reno-ect0')
        self.assertEqual(case['background_profile'],'bbr-prague-unlimited')
        self.assertTrue(case['id'].endswith('-bg-bbr-prague'))

    def test_bbr_is_one_distinct_configured_sample(self):
        self.config_file.write_text(self.config_file.read_text().replace(
            'path = "ideal"', 'path = "qemu"\ncontrollers = ["bbr"]')
            .replace('reruns = 2','reruns = 1').replace('delays_ms = [160, 320]','delays_ms = [15]'))
        cfg=self.load()
        cases=self.config.expand_cases(cfg)
        self.assertEqual(len(cases),1)
        self.assertEqual(cases[0]['controller'],'bbr')
        command=self.runner.build_command(cases[0],cfg,self.root/'results/out',self.root/'scaled.json',self.root/'topology.py')
        self.assertEqual(command[command.index('--transport-mode')+1],'bbr')
        self.assertEqual(cases[0]['background_profile'],'bbr-prague-unlimited')

    def test_default_coupling_is_three(self):
        self.assertEqual(self.load()['experiments'][0]['dualpi2_coupling_factor'],3)

    def test_removed_topology_options_fail_clearly(self):
        original=self.config_file.read_text()
        for setting in ('switches = 3','background = false','background_cc = "bbr"',
                        'background_link_delay_ms = 50','qemu_delay_mode = "per-link"'):
            self.config_file.write_text(original.replace('path = "ideal"', 'path = "qemu"\n'+setting))
            with self.subTest(setting=setting), self.assertRaisesRegex(ValueError,'unknown'):
                self.load()

    def test_qemu_defaults_to_one_prague_scene_with_fixed_mixed_backgrounds(self):
        self.config_file.write_text(self.config_file.read_text().replace('path = "ideal"','path = "qemu"'))
        cases=self.config.expand_cases(self.load())
        self.assertEqual({c['controller'] for c in cases},{'prague'})
        self.assertEqual({c['switches'] for c in cases},{2})
        self.assertEqual({c['background_profile'] for c in cases},{'bbr-prague-unlimited'})

    def test_runner_generates_shared_background_topology_and_configurable_aqm(self):
        cfg=self.load()
        cfg['experiments'][0].update(path='qemu',controllers=['reno'],reruns=1,delays_ms=[160],
                                     background_warmup_seconds=6,dualpi2_target_ms=10,
                                     dualpi2_update_ms=20,dualpi2_step_ms=2,dualpi2_coupling_factor=2)
        cfg['qemu']['performance_mode']=True
        ledger,calls=self.execute(cfg,[{'status':'PASS','video_status':'PASS','quality_status':'PASS'}])
        item=ledger['items'][0]
        self.assertEqual(item['background_profile'],'bbr-prague-unlimited')
        command=calls[0]
        source=Path(command[command.index('--topology-script')+1]).read_text()
        namespace={}
        exec(compile(source,'<generated-topology>','exec'),namespace)
        self.assertEqual(namespace['BACKGROUND_WARMUP_SECONDS'],6)
        self.assertEqual(namespace['DUALPI2_TARGET_MS'],10)
        self.assertEqual(namespace['DUALPI2_UPDATE_MS'],20)
        self.assertEqual(namespace['DUALPI2_STEP_MS'],2)
        self.assertEqual(namespace['DUALPI2_COUPLING_FACTOR'],2)
        self.assertTrue(namespace['PERFORMANCE_MODE'])
        self.assertTrue(command[command.index('--qemu-runner')+1].endswith('/qemu_player_performance.py'))
        self.assertEqual([p['cc'] for p in namespace['background_pairs']()],['bbr','prague'])

    def test_invalid_l4s_parameters_are_rejected(self):
        original=self.config_file.read_text()
        for field in ('dualpi2_target_ms','dualpi2_update_ms','dualpi2_step_ms','dualpi2_coupling_factor'):
            for value in ('nan','inf','-1','true'):
                self.config_file.write_text(original.replace('path = "ideal"', f'path = "qemu"\n{field} = {value}'))
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):self.load()
        for value in ('0','1.5','256'):
            self.config_file.write_text(original.replace('path = "ideal"',f'path = "qemu"\ndualpi2_coupling_factor = {value}'))
            with self.subTest(value=value), self.assertRaises(ValueError):self.load()

    def test_scaling_preserves_all_spatial_data(self):
        report = self.runner.scale_trace(self.root/'trace.json', self.root/'scaled.json', 5)
        trace = json.loads((self.root/'scaled.json').read_text())
        self.assertEqual(trace[-1]['timestamp_ms'], 5000)
        self.assertEqual(report['playback_speed_factor'], 3.8)
        self.assertEqual([f['camera_position'] for f in trace], [f['camera_position'] for f in self.source])

    def test_virtualenv_python_entrypoint_is_not_resolved_to_system_python(self):
        interpreter=self.root/'venv/bin/python'
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        self.config_file.write_text(self.config_file.read_text().replace('[runner]',
            '[runner]\npython = "venv/bin/python"'))
        self.assertEqual(self.load()['runner']['python'],str(interpreter))

    def test_qemu_command_is_explicit_and_topology_not_ideal(self):
        cfg = self.load(); cfg['experiments'][0].update(path='qemu', controllers=['reno','prague'])
        cases = self.config.expand_cases(cfg)
        self.assertEqual(len(cases),8)
        command=self.runner.build_command(cases[0],cfg,self.root/'results/out',self.root/'scaled.json',self.root/'topology.py')
        self.assertEqual(command[command.index('--network-path')+1],'qemu')
        self.assertIn('--topology-script',command)
        self.assertNotIn('--ideal-data-delay-ms',command)

    def test_unknown_keys_and_nonfinite_or_invalid_values_are_rejected(self):
        original=self.config_file.read_text()
        for before,after in [('bandwidth_mbps = 50','bandwidth_mbps = -1'),
                             ('reruns = 2','reruns = 0'),
                             ('path = "ideal"','path = "typo"'),
                             ('duration_seconds = 5','duration_seconds = nan'),
                             ('on_failure = "continue"','on_failure = "ignore"')]:
            with self.subTest(after=after):
                self.config_file.write_text(original.replace(before,after))
                with self.assertRaises(ValueError): self.load()
        self.config_file.write_text(original+'typo = 7\n')
        with self.assertRaisesRegex(ValueError,'unknown'): self.load()

    def test_dry_run_does_not_create_output_or_execute(self):
        cfg=self.load()
        plan=self.runner.make_plan(cfg)
        self.assertEqual(len(plan['items']),2)
        self.assertFalse(Path(cfg['output']).exists())

    def execute(self,cfg,results):
        calls=[]
        def executor(command,log,timeout):
            output=Path(command[command.index('--output')+1]); output.mkdir(parents=True)
            status=results[min(len(calls),len(results)-1)];calls.append(command)
            (output/'validation-summary.json').write_text(json.dumps(status))
            if status.get('video_status')=='PASS': (output/'camera-trace.mp4').write_bytes(b'fixture')
            log.write_text('fixture\n')
            return 0 if status.get('status')=='PASS' else 1
        ledger=self.runner.run_batch(cfg,executor=executor)
        return ledger,calls

    def test_retry_preserves_attempts_and_selects_first_criterion_pass(self):
        cfg=self.load();cfg['experiments'][0].update(reruns=1,delays_ms=[160],on_failure='retry',max_retries=2)
        fail={'status':'FAIL','video_status':'FAIL','quality_status':'FAIL','delivery_status':'FAIL'}
        partial={'status':'FAIL','video_status':'PASS','quality_status':'PASS','delivery_status':'FAIL'}
        ledger,calls=self.execute(cfg,[fail,partial])
        self.assertEqual(len(calls),2)
        item=ledger['items'][0]
        self.assertEqual(len(item['attempts']),2)
        self.assertEqual(item['selected_attempt'],2)
        self.assertEqual(ledger['status'],'PARTIAL')
        self.assertEqual(ledger['execution_status'],'complete')
        self.assertTrue((Path(cfg['output'])/item['attempts'][0]['directory']).exists())

    def test_stop_does_not_start_later_items(self):
        cfg=self.load();cfg['experiments'][0]['on_failure']='stop'
        ledger,calls=self.execute(cfg,[{'video_status':'FAIL'}])
        self.assertEqual(len(calls),1)
        self.assertEqual(ledger['execution_status'],'stopped')

    def test_strict_criterion_retries_delivery_failure_without_relabeling_pass(self):
        cfg=self.load();cfg['experiments'][0].update(reruns=1,delays_ms=[160],on_failure='retry',max_retries=1,failure_criterion='all')
        ledger,calls=self.execute(cfg,[{'status':'FAIL','video_status':'PASS','quality_status':'PASS','delivery_status':'FAIL'}])
        self.assertEqual(len(calls),2)
        self.assertFalse(ledger['items'][0]['attempts'][-1]['criterion_pass'])
        self.assertEqual(ledger['status'],'FAIL')
        self.assertNotIn('timing_status', ledger['items'][0]['attempts'][-1])

    def test_interrupt_is_written_and_not_retried(self):
        cfg=self.load()
        def executor(command,log,timeout): raise KeyboardInterrupt()
        ledger=self.runner.run_batch(cfg,executor=executor)
        self.assertEqual(ledger['execution_status'],'interrupted')
        stored=json.loads((Path(cfg['output'])/'ledger.json').read_text())
        self.assertEqual(stored['execution_status'],'interrupted')

    def test_executor_owner_is_closed_after_interruption(self):
        cfg=self.load()
        class Executor:
            closed=False
            def __call__(self,*args):raise KeyboardInterrupt()
            def close(self):self.closed=True
        executor=Executor()
        ledger=self.runner.run_batch(cfg,executor=executor)
        self.assertTrue(executor.closed,'batch must release its VM owner on interruption')
        self.assertEqual(ledger['execution_status'],'interrupted')

    def test_dirty_vm_reset_stops_batch_even_when_failures_normally_continue(self):
        from qemu_session import ResetFailure
        cfg=self.load();calls=[]
        def executor(*args):
            calls.append(args)
            raise ResetFailure('dirty VM')
        ledger=self.runner.run_batch(cfg,executor=executor)
        self.assertEqual(len(calls),1)
        self.assertEqual(ledger['execution_status'],'stopped')
        self.assertIn('ResetFailure',ledger['setup_error'])
        self.assertEqual(ledger['items'][0]['attempts'][0]['status'],'FAIL')
        self.assertEqual(ledger['items'][1]['attempts'],[])

    def test_existing_output_is_never_overwritten(self):
        cfg=self.load();Path(cfg['output']).mkdir()
        with self.assertRaises(FileExistsError):self.runner.run_batch(cfg,executor=lambda *_:0)

    def test_successful_replot_clears_previous_analysis_error(self):
        cfg=self.load()
        ledger,calls=self.execute(cfg,[{'video_status':'FAIL'}])
        ledger['analysis_error']='old failure'
        with patch('experiment_suite_analysis.analyze_batch',return_value={'status':'PASS','groups':[]}):
            self.runner.analyze(Path(cfg['output']),ledger)
        self.assertNotIn('analysis_error',ledger)
        self.assertFalse(self.runner.analysis_failed(ledger))

    def test_blocked_requested_metric_is_cli_failure_despite_partial_evidence(self):
        ledger={'analysis':{'status':'PARTIAL','groups':[{'vmaf_status':'BLOCKED'}]}}
        self.assertTrue(self.runner.analysis_failed(ledger))
        ledger['analysis']['groups']=[{'vmaf_status':'PASS','plots_status':'PASS'}]
        self.assertFalse(self.runner.analysis_failed(ledger))

    def test_timeout_is_recorded_and_retry_exhaustion_continues(self):
        import subprocess
        cfg=self.load()
        cfg['experiments'][0].update(reruns=1,on_failure='retry',max_retries=1)
        def executor(command,log,timeout):raise subprocess.TimeoutExpired(command,timeout)
        ledger=self.runner.run_batch(cfg,executor=executor)
        self.assertEqual(ledger['execution_status'],'complete')
        self.assertEqual(len(ledger['items']),2)
        self.assertTrue(all(len(item['attempts'])==2 for item in ledger['items']))
        self.assertTrue(all('TimeoutExpired' in item['attempts'][-1]['error'] for item in ledger['items']))
        self.assertEqual(ledger['status'],'FAIL')


if __name__=='__main__': unittest.main()
