"""Offline suite analysis: selection, exact alignment, and explicit backend failures."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'tools/l4s/experiment_suite_analysis.py'
spec = importlib.util.spec_from_file_location('suite_analysis', MODULE)
analysis = importlib.util.module_from_spec(spec)
if MODULE.exists():
    spec.loader.exec_module(analysis)


class SuiteAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.batch = Path(self.tmp.name)
        self.settings = {'enabled': True, 'vmaf': False, 'plots': False}

    def item(self, name='one', selected=0):
        run = self.batch / name
        (run / 'quality').mkdir(parents=True)
        (run / 'quality/frame-quality.csv').write_text(
            'timestamp_ms,rgb_ssim,psnr_db,applied_gaussians\n0,0.5,20,5\n1000,0.9,30,9\n')
        (run / 'player-telemetry.json').write_text(json.dumps({
            'sourceGaussians': 10, 'trace': {'startedAtMs': 200}, 'events': [
                {'type': 'object', 'timestampMs': 200, 'payload_bytes': 100000},
                {'type': 'object', 'timestampMs': 1200, 'payload_bytes': 200000}]}))
        return {'id': name, 'scene': 'train', 'bandwidth_mbps': 50, 'delay_ms': 160,
                'duration_seconds': 2, 'path': 'ideal', 'controller': 'ideal',
                'switches': 1, 'rerun': 0, 'experiment_index': 0,
                'attempts': [{'index': 0, 'directory': name, 'capture_status': 'PASS',
                              'quality_status': 'PASS', 'timing_status': 'FAIL', 'status': 'FAIL'}],
                'selected_attempt': selected}

    def test_receipt_clock_precedes_worker_completion_with_explicit_legacy_fallback(self):
        bins = analysis.browser_bins({'trace': {'startedAtMs': 200}, 'events': [
            {'type': 'object', 'receivedAtMs': 300, 'timestampMs': 1500, 'payload_bytes': 10},
            {'type': 'object', 'timestampMs': 1400, 'payload_bytes': 20}]}, 2)
        self.assertEqual([row['payload_bytes'] for row in bins], [10, 20])
        self.assertEqual(bins[0]['receipt_timestamp_events'], 1)
        self.assertEqual(bins[1]['legacy_completion_timestamp_events'], 1)

    def paired_member(self, name, controller='ideal', delay=160, rerun=0,
                      timestamps=(0, 1000), ssim=.5, vmaf=60):
        from PIL import Image
        folder = self.batch / name
        folder.mkdir(exist_ok=True)
        pairs = {}
        for timestamp in timestamps:
            image = folder / f'{timestamp}.png'
            Image.new('RGB', (2, 2), (0, 0, 0)).save(image)
            frame = {'cameraPose': {'x': timestamp}, '_path': image}
            pairs[timestamp] = (frame, frame)
        run = {'id': name, 'controller': controller, 'delay_ms': delay, 'rerun': rerun,
               'path': 'ideal' if controller == 'ideal' else 'qemu', 'status': 'PARTIAL',
               'vmaf_status': 'PASS', 'vmaf_timestamps_ms': list(timestamps),
               'vmaf_rows': [{'timestamp_ms': t, 'vmaf': vmaf} for t in timestamps]}
        quality = [{'timestamp_ms': t, 'rgb_ssim': ssim} for t in timestamps]
        return run, quality, [], pairs

    def test_qemu_groups_fix_delay_and_pairs_never_cross_repeats(self):
        items = []
        for delay in (160, 320):
            for controller in ('reno', 'prague'):
                item = self.item(f'{controller}-{delay}')
                item.update(path='qemu', controller=controller, delay_ms=delay)
                items.append(item)
        result = analysis.analyze_batch(self.batch, {'items': items}, self.settings)
        self.assertEqual(len(result['groups']), 2)
        for group in result['groups']:
            self.assertEqual(group['differences'][0]['label'], 'Prague − Reno')
            self.assertEqual(group['differences'][0]['ssim']['status'], 'UNAVAILABLE')
        a = self.paired_member('p', 'prague', rerun=0)
        b = self.paired_member('r', 'reno', rerun=1)
        differences = analysis.quality_differences([a, b])
        self.assertEqual(len(differences), 2)
        self.assertTrue(all(d['ssim']['status'] == 'UNAVAILABLE' for d in differences))

    def test_difference_sign_failed_members_and_exact_pose_alignment(self):
        high = self.paired_member('high', delay=320, ssim=.4, vmaf=50)
        low = self.paired_member('low', delay=160, ssim=.7, vmaf=70)
        difference = analysis.quality_differences([low, high])[0]
        self.assertEqual(difference['label'], '320 ms − 160 ms')
        self.assertEqual(difference['minuend_status'], 'PARTIAL')
        self.assertAlmostEqual(difference['ssim']['mean'], -.3)
        self.assertEqual(difference['vmaf']['mean'], -20)
        low[3][1000][0]['cameraPose'] = {'x': 99}
        difference = analysis.quality_differences([high, low])[0]
        self.assertEqual(difference['ssim']['count'], 1)
        self.assertEqual(difference['vmaf']['status'], 'UNAVAILABLE')

    def test_vmaf_requires_full_identical_grid_and_reference_rgb(self):
        from PIL import Image
        high = self.paired_member('high', delay=80, timestamps=(0, 1000))
        low = self.paired_member('low', delay=40, timestamps=(0,))
        difference = analysis.quality_differences([high, low])[0]
        self.assertEqual(difference['ssim']['count'], 1)
        self.assertEqual(difference['vmaf']['status'], 'UNAVAILABLE')
        self.assertIn('grid', difference['vmaf']['reason'])
        Image.new('RGB', (2, 2), (255, 0, 0)).save(low[3][0][1]['_path'])
        difference = analysis.quality_differences([high, low])[0]
        self.assertEqual(difference['ssim']['count'], 0)
        self.assertEqual(difference['ssim']['status'], 'UNAVAILABLE')

    def test_all_delay_pairs_have_descending_sign_and_missing_repeats_are_visible(self):
        members = [self.paired_member(str(delay), delay=delay, ssim=delay / 1000)
                   for delay in (40, 80, 160)]
        members.append(self.paired_member('repeat', delay=40, rerun=1))
        pairs = analysis.quality_differences(members)
        self.assertEqual([pair['label'] for pair in pairs[:3]],
                         ['160 ms − 80 ms', '160 ms − 40 ms', '80 ms − 40 ms'])
        self.assertTrue(all(pair['ssim']['mean'] > 0 for pair in pairs[:3]))
        self.assertEqual(len(pairs), 6)
        self.assertTrue(all(pair['ssim']['status'] == 'UNAVAILABLE' for pair in pairs[3:]))

    def test_qemu_sign_is_prague_minus_reno_and_ambiguous_members_are_unavailable(self):
        reno = self.paired_member('reno', controller='reno', ssim=.8, vmaf=90)
        prague = self.paired_member('prague', controller='prague', ssim=.9, vmaf=95)
        difference = analysis.quality_differences([reno, prague])[0]
        self.assertEqual(difference['minuend_id'], 'prague')
        self.assertAlmostEqual(difference['ssim']['mean'], .1)
        self.assertEqual(difference['vmaf']['mean'], 5)
        duplicate = self.paired_member('duplicate', controller='prague')
        difference = analysis.quality_differences([reno, prague, duplicate])[0]
        self.assertEqual(difference['ssim']['status'], 'UNAVAILABLE')
        self.assertEqual(difference['minuend_status'], 'MISSING_OR_AMBIGUOUS')

    def test_selected_source_failures_cannot_be_upgraded_to_pass(self):
        for field, value in [('status', 'PARTIAL'), ('process_exit_code', 7), ('criterion_pass', False)]:
            with self.subTest(field=field):
                item = self.item(name=field)
                item['attempts'][0].update(status='PASS', timing_status='PASS',
                                           process_exit_code=0, criterion_pass=True)
                item['attempts'][0][field] = value
                result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
                self.assertNotEqual(result['status'], 'PASS')
                self.assertEqual(result['runs'][0]['source_attempt_status'], item['attempts'][0]['status'])
        item = self.item(name='validation')
        item['attempts'][0].update(status='PASS', timing_status='PASS', process_exit_code=0, criterion_pass=True)
        (self.batch / 'validation/validation-summary.json').write_text(json.dumps({'status': 'PARTIAL'}))
        result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
        self.assertEqual(result['runs'][0]['source_validation_status'], 'PARTIAL')
        self.assertNotEqual(result['status'], 'PASS')

    def test_requested_grid_tolerates_float_roundoff_only(self):
        self.assertEqual(analysis.missing_requested_timestamps([0, 1000 / 3 + 1e-10, 2000 / 3], 3, 3), [])
        self.assertEqual(analysis.missing_requested_timestamps([0, 334, 2000 / 3], 3, 3), [1000 / 3])

    def test_selection_is_explicit_and_timing_failure_is_preserved(self):
        item = self.item()
        item['attempts'].append({'index': 1, 'directory': 'nonexistent', 'status': 'PASS'})
        result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
        run = result['runs'][0]
        self.assertEqual(run['selected_attempt'], 0)
        self.assertEqual(run['timing_status'], 'FAIL')
        self.assertEqual(run['quality_frames'], 2)
        self.assertAlmostEqual(run['mean_browser_payload_mbps'], 1.2)
        self.assertNotEqual(result['status'], 'PASS')
        self.assertTrue((self.batch / 'analysis/summary.csv').is_file())

    def test_qemu_not_applicable_timing_is_preserved_without_failing_analysis(self):
        item = self.item()
        item['path'] = 'qemu'
        item['controller'] = 'reno'
        item['attempts'][0]['timing_status'] = 'NOT_APPLICABLE'
        item['attempts'][0]['status'] = 'PASS'
        result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
        self.assertEqual(result['runs'][0]['timing_status'], 'NOT_APPLICABLE')
        self.assertEqual(result['status'], 'PASS')

    def test_background_conditions_are_preserved_and_never_grouped_together(self):
        a=self.item('a');b=self.item('b')
        a.update(background=True,background_cc='cubic')
        b.update(background=True,background_cc='reno')
        result=analysis.analyze_batch(self.batch,{'items':[a,b]},self.settings)
        self.assertEqual(len(result['groups']),2)
        self.assertEqual({r['background_cc'] for r in result['runs']},{'cubic','reno'})

    def test_no_selected_attempt_does_not_pick_success(self):
        result = analysis.analyze_batch(self.batch, {'items': [self.item(selected=None)]}, self.settings)
        self.assertEqual(result['runs'][0]['quality_frames'], 0)
        self.assertEqual(result['runs'][0]['status'], 'BLOCKED')

    def test_missing_vmaf_tool_keeps_ssim_and_reports_blocked(self):
        settings = dict(self.settings, vmaf=True, vmaf_backend='cuda',
                        vmaf_binary='/no/such/vmaf', vmaf_model='/no/such/model')
        result = analysis.analyze_batch(self.batch, {'items': [self.item()]}, settings)
        self.assertEqual(result['groups'][0]['vmaf_status'], 'BLOCKED')
        self.assertEqual(result['runs'][0]['quality_frames'], 2)

    def test_exact_pose_intersection_and_missing_refs(self):
        a = {0: ({'cameraPose': {'x': 1}}, {}), 1: ({'cameraPose': {'x': 2}}, {})}
        b = {0: ({'cameraPose': {'x': 1}}, {}), 1: ({'cameraPose': {'x': 3}}, {})}
        self.assertEqual(analysis.common_poses([a, b]), [0])
        self.assertEqual(analysis.common_poses([a, {}]), [])

    def test_recorded_cpu_only_vmaf_build_rejects_cuda_request(self):
        build = self.batch / 'build'
        (build / 'tools').mkdir(parents=True)
        (build / 'meson-info').mkdir()
        binary = build / 'tools/vmaf'
        binary.write_bytes(b'fixture')
        model = self.batch / 'model.json'
        model.write_text('{}')
        (build / 'meson-info/intro-buildoptions.json').write_text(json.dumps([
            {'name': 'enable_cuda', 'value': False}]))
        result = analysis.vmaf_group(self.batch, 'analysis', [], [], {
            'vmaf_binary': str(binary), 'vmaf_model': str(model), 'vmaf_backend': 'cuda'})
        self.assertEqual(result['vmaf_status'], 'BLOCKED')
        self.assertFalse(result['build_cuda_enabled'])
        self.assertIn('CUDA disabled', result['vmaf_error'])

    def test_cuda_fallback_is_rejected_and_command_retained(self):
        out = self.batch / 'analysis'
        out.mkdir()
        commands = []
        with patch.object(analysis.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b'CUDA unavailable, using CPU fallback'
            with self.assertRaisesRegex(RuntimeError, 'fallback'):
                analysis.run_command(['vmaf'], out / 'run.log', commands, backend='cuda')
        self.assertEqual(commands[0]['argv'], ['vmaf'])
        self.assertTrue((out / 'run.log').is_file())

    def test_actual_frame_timing_is_retained_without_vmaf(self):
        item = self.item()
        for kind in ('candidate', 'reference'):
            folder = self.batch / 'one' / f'{kind}-frames'
            folder.mkdir()
            (folder / 'frame.png').write_bytes(b'fixture-only-no-image-decoding')
            (folder / 'frames.json').write_text(json.dumps({
                'complete': True, 'requestedFps': 1, 'cameraTimestamps': 'fixed', 'frames': [{
                    'file': 'frame.png', 'timestampMs': 0, 'cameraPose': {'x': 1},
                    'actualElapsedMs': 52, 'appliedGaussians': 5}]}))
        result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
        rows = result['runs'][0]['frame_timing']
        self.assertEqual(rows[0]['actual_elapsed_ms'], 52)
        self.assertEqual(rows[0]['snapshot_lateness_ms'], 52)
        self.assertEqual(result['runs'][0]['missing_requested_timestamps_ms'], [1000])

    def test_missing_references_keep_candidate_actual_timings(self):
        item = self.item()
        folder = self.batch / 'one/candidate-frames'
        folder.mkdir()
        (folder / 'frame.png').write_bytes(b'fixture')
        (folder / 'frames.json').write_text(json.dumps({'frames': [{
            'file': 'frame.png', 'timestampMs': 0, 'cameraPose': {'x': 1}, 'actualElapsedMs': 90}]}))
        result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
        self.assertEqual(result['runs'][0]['frame_timing'][0]['actual_elapsed_ms'], 90)
        self.assertEqual(result['groups'][0]['common_pose_count'], 0)

    def test_no_selected_run_is_excluded_from_common_intersection(self):
        item = self.item()
        other = self.item('unselected', selected=None)
        for kind in ('candidate', 'reference'):
            folder = self.batch / 'one' / f'{kind}-frames'
            folder.mkdir()
            (folder / 'frame.png').write_bytes(b'fixture')
            (folder / 'frames.json').write_text(json.dumps({'frames': [{
                'file': 'frame.png', 'timestampMs': 0, 'cameraPose': {'x': 1}}]}))
        result = analysis.analyze_batch(self.batch, {'items': [item, other]}, self.settings)
        self.assertEqual(result['groups'][0]['common_pose_count'], 1)
        self.assertNotEqual(result['status'], 'PASS')

    def test_vmaf_native_sequence_scores_and_provenance(self):
        from PIL import Image
        item = self.item()
        for kind in ('candidate', 'reference'):
            folder = self.batch / 'one' / f'{kind}-frames'
            folder.mkdir()
            frames = []
            for index in range(2):
                Image.new('RGB', (8, 8), (index * 10, 0, 0)).save(folder / f'{index}.png')
                frames.append({'file': f'{index}.png', 'timestampMs': index * 1000,
                               'cameraPose': {'x': index}, 'actualElapsedMs': index * 1000 + 40})
            (folder / 'frames.json').write_text(json.dumps({'complete': True, 'requestedFps': 1, 'frames': frames}))
        binary, model = self.batch / 'vmaf', self.batch / 'model.json'
        binary.write_bytes(b'fixture binary')
        model.write_text('{}')
        settings = dict(self.settings, vmaf=True, vmaf_backend='cpu',
                        vmaf_binary=str(binary), vmaf_model=str(model))
        def command(argv, **kwargs):
            if argv[0] == 'ffmpeg':
                Path(argv[-1]).write_bytes(bytes(2 * 8 * 8 * 3 // 2))
            else:
                Path(argv[argv.index('--output') + 1]).write_text(json.dumps({
                    'frames': [{'metrics': {'vmaf': 60}}, {'metrics': {'vmaf': 80}}]}))
            import subprocess
            return subprocess.CompletedProcess(argv, 0, stdout=b'')
        with patch.object(analysis.subprocess, 'run', side_effect=command):
            result = analysis.analyze_batch(self.batch, {'items': [item]}, settings)
        group = result['groups'][0]
        self.assertEqual(group['vmaf_status'], 'PASS')
        self.assertTrue(group['rgb_references_equal'])
        self.assertEqual(group['native_dimensions'], [8, 8])
        self.assertEqual(result['runs'][0]['mean_vmaf'], 70)
        self.assertEqual(result['status'], 'PARTIAL')  # source timing still FAIL
        commands = json.loads((self.batch / 'analysis/group-0000/commands.json').read_text())
        self.assertEqual(commands[-1]['argv'][commands[-1]['argv'].index('--gpumask') + 1], '-1')
        self.assertEqual(commands[-1]['argv'][commands[-1]['argv'].index('--threads') + 1], '0')
        self.assertTrue((self.batch / 'analysis/group-0000/input-checksums.json').is_file())
        self.assertFalse(list((self.batch / 'analysis').rglob('*.yuv')))

    def test_missing_reference_does_not_block_other_run_vmaf(self):
        usable = self.item('usable')
        missing = self.item('missing')
        for kind in ('candidate', 'reference'):
            folder = self.batch / 'usable' / f'{kind}-frames'
            folder.mkdir()
            (folder / 'frame.png').write_bytes(b'fixture')
            (folder / 'frames.json').write_text(json.dumps({'frames': [{
                'file': 'frame.png', 'timestampMs': 0, 'cameraPose': {'x': 1}}]}))
        def evaluate(out, name, members, grid, settings):
            self.assertEqual([member[0]['id'] for member in members], ['usable'])
            self.assertEqual(grid, [0])
            members[0][0].update(mean_vmaf=70, vmaf_frames=1)
            return {'vmaf_status': 'PASS', 'scores': {'usable': {'mean': 70}}}
        with patch.object(analysis, 'vmaf_group', side_effect=evaluate):
            result = analysis.analyze_batch(self.batch, {'items': [usable, missing]},
                                            dict(self.settings, vmaf=True))
        self.assertEqual(result['groups'][0]['vmaf_status'], 'PARTIAL')
        self.assertEqual(result['runs'][0]['vmaf_timestamps_ms'], [0])
        self.assertEqual(result['runs'][1]['vmaf_status'], 'MISSING_EVIDENCE')
        self.assertFalse(result['groups'][0]['vmaf_means_comparable'])

    def test_duration_excludes_endpoint_from_expected_sample_count(self):
        item = self.item()
        item['duration_seconds'] = 19
        folder = self.batch / 'one/candidate-frames'
        folder.mkdir()
        (folder / 'frames.json').write_text(json.dumps({'requestedFps': 5, 'frames': []}))
        result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
        self.assertEqual(result['runs'][0]['expected_quality_samples'], 95)

    def test_accepted_snapshot_loss_preserves_source_acceptance(self):
        item = self.item()
        item['attempts'][0].update(status='PASS', timing_status='PASS')
        (self.batch / 'one/quality/comparison.json').write_text(json.dumps({
            'snapshot_coverage': {'accepted': True, 'max_missing_fraction': .05,
                                  'coverage': 94 / 95, 'complete': False}}))
        for kind in ('candidate', 'reference'):
            folder = self.batch / 'one' / f'{kind}-frames'
            folder.mkdir()
            (folder / 'image.png').write_bytes(b'fixture')
            (folder / 'frames.json').write_text(json.dumps({
                'complete': kind == 'reference', 'requestedFps': 1,
                'cameraTimestamps': 'fixed', 'frames': [{
                    'file': 'image.png', 'timestampMs': 0, 'cameraPose': {'x': 1}}]}))
        with patch.object(analysis, 'vmaf_group', return_value={
                'vmaf_status': 'PASS', 'rgb_references_equal': True}):
            result = analysis.analyze_batch(self.batch, {'items': [item]},
                                            dict(self.settings, vmaf=True))
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['runs'][0]['max_missing_fraction'], .05)
        self.assertEqual(result['runs'][0]['missing_requested_timestamps_ms'], [1000])

    def test_failed_selected_run_still_produces_plots(self):
        try:
            import matplotlib
        except ImportError:
            self.skipTest('matplotlib unavailable')
        item = self.item()
        item['attempts'][0].update(capture_status='FAIL', quality_status='INCOMPLETE')
        result = analysis.analyze_batch(self.batch, {'items': [item]},
                                        dict(self.settings, plots=True))
        self.assertEqual(result['groups'][0]['plots_status'], 'PASS')
        self.assertNotEqual(result['status'], 'PASS')
        self.assertTrue((self.batch / 'analysis/group-0000.png').is_file())
        self.assertTrue((self.batch / 'analysis/group-0000.pdf').is_file())

    def test_attempt_path_cannot_escape_batch(self):
        item = self.item()
        item['attempts'][0]['directory'] = '../escape'
        result = analysis.analyze_batch(self.batch, {'items': [item]}, self.settings)
        self.assertEqual(result['runs'][0]['status'], 'BLOCKED')
        self.assertIn('outside batch', result['runs'][0]['error'])


if __name__ == '__main__':
    unittest.main()
