"""Offline cache lifecycle and source invalidation tests (no QEMU boot)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools/l4s'
sys.path.insert(0, str(TOOLS))


class SetupCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def module(self):
        self.assertTrue((TOOLS / 'qemu_setup_cache.py').exists(), 'setup cache helper must exist')
        import qemu_setup_cache
        return qemu_setup_cache

    def test_content_key_includes_dirty_untracked_and_deletions_but_ignores_mtime(self):
        module = self.module()
        repo = self.root / 'dependency'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        source = repo / 'source.c'
        source.write_text('original')
        subprocess.run(['git', 'add', 'source.c'], cwd=repo, check=True)
        import run_qemu_player as runner
        archive = self.root / 'dependency.tar.gz'
        def fingerprint():
            runner.archive_worktree(repo, archive)
            return module.archive_digest(archive)
        original = fingerprint()
        os.utime(source, (100, 100))
        self.assertEqual(original, fingerprint())
        source.write_text('dirty')
        dirty = fingerprint()
        self.assertNotEqual(original, dirty)
        extra = repo / 'new.c'
        extra.write_text('untracked')
        self.assertNotEqual(dirty, fingerprint())
        extra.unlink()
        source.unlink()
        self.assertNotEqual(original, fingerprint())

    def test_key_covers_recipe_base_architecture_and_picotls(self):
        module = self.module()
        config = {'base': {'version': 1}, 'arch': 'x86_64', 'recipe': 'a', 'picotls': 'hash'}
        key = module.cache_key(config)
        self.assertEqual(key, module.cache_key(dict(reversed(list(config.items())))))
        for field in config:
            changed = dict(config, **{field: 'changed'})
            self.assertNotEqual(key, module.cache_key(changed))

    def test_failed_preparation_never_published_and_hit_does_not_rebuild(self):
        module = self.module()
        cache = module.SetupCache(self.root / 'cache', {'sources': 'first'})
        attempts = []
        def failure(image):
            image.write_bytes(b'partial')
            raise RuntimeError('preparation failed')
        with self.assertRaisesRegex(RuntimeError, 'preparation failed'):
            with cache.locked(): cache.obtain(failure)
        self.assertFalse(cache.entry.exists())
        def prepare(image):
            attempts.append(image)
            image.write_bytes(b'clean shutdown image')
        with cache.locked():
            image, hit = cache.obtain(prepare)
        self.assertFalse(hit)
        self.assertEqual(image.read_bytes(), b'clean shutdown image')
        with cache.locked():
            reused, hit = cache.obtain(lambda _: self.fail('hit must not prepare'))
        self.assertTrue(hit)
        self.assertEqual(image, reused)
        self.assertEqual(len(attempts), 1)
        metadata = json.loads((cache.entry / 'metadata.json').read_text())
        self.assertEqual(metadata['key'], cache.key)
        self.assertEqual(metadata['configuration'], {'sources': 'first'})

    def test_incomplete_cache_is_rejected_without_reuse_or_mutation(self):
        module = self.module()
        cache = module.SetupCache(self.root / 'cache', {'source': 1})
        cache.entry.mkdir(parents=True)
        (cache.entry / 'setup.qcow2').write_bytes(b'incomplete')
        with self.assertRaisesRegex(RuntimeError, 'invalid setup cache'):
            with cache.locked():
                cache.obtain(lambda _: self.fail('must not replace existing cache'))
        self.assertEqual((cache.entry / 'setup.qcow2').read_bytes(), b'incomplete')

    def test_archive_key_ignores_headers_but_includes_content_and_mode(self):
        module = self.module()
        source = self.root / 'source.c'
        source.write_text('content')
        archive = self.root / 'source.tar.gz'
        def fingerprint(timestamp):
            with tarfile.open(archive, 'w:gz') as output:
                info = output.gettarinfo(str(source), arcname='source.c')
                info.mtime = timestamp
                with source.open('rb') as stream: output.addfile(info, stream)
            return module.archive_digest(archive)
        original = fingerprint(1)
        self.assertEqual(original, fingerprint(123456))
        source.chmod(0o755)
        self.assertNotEqual(original, fingerprint(1))
        source.chmod(0o644)
        source.write_text('changed')
        self.assertNotEqual(original, fingerprint(1))

    def test_runtime_stages_current_inputs_preserving_native_builds(self):
        import run_qemu_player as runner
        guest = self.root / 'guest'
        (guest / 'deps/imquic').mkdir(parents=True)
        binary = guest / 'deps/imquic/compiled-relay'
        binary.write_text('cached binary')
        (guest / 'stale-input').write_text('old scene')
        (guest / 'deps/gaussian-player').mkdir()
        (guest / 'deps/gaussian-player/stale').write_text('old player')
        current = self.root / 'current.py'
        current.write_text('new root')
        player = self.root / 'player.js'
        player.write_text('new player')
        for name, source in (('root.tar.gz', current), ('player.tar.gz', player)):
            with tarfile.open(self.root / name, 'w:gz') as archive:
                archive.add(source, arcname=source.name)
        (self.root / 'provenance.json').write_text('{}')
        script = runner.stage_runtime_script(str(guest), 'testuser', 'root.tar.gz',
                                              'player.tar.gz', 'provenance.json', '')
        script = script.replace('/home/testuser/', str(self.root) + '/')
        subprocess.run(['bash', '-c', script], check=True, capture_output=True)
        self.assertEqual(binary.read_text(), 'cached binary')
        self.assertEqual((guest / 'current.py').read_text(), 'new root')
        self.assertEqual((guest / 'deps/gaussian-player/player.js').read_text(), 'new player')
        self.assertFalse((guest / 'stale-input').exists())
        self.assertFalse((guest / 'deps/gaussian-player/stale').exists())

    def test_unclean_poweroff_cannot_publish_preparation(self):
        import run_qemu_player as runner
        module = self.module()
        cache = module.SetupCache(self.root / 'cache', {'lifecycle': 1})
        process = Mock()
        process.poll.side_effect = [None, None]
        process.wait.side_effect = [subprocess.TimeoutExpired('qemu', 60), 0]
        args = SimpleNamespace(user='user', password='test-only')
        def prepare(image):
            image.write_bytes(b'not cleanly powered off')
            with patch.object(runner, 'ssh_command'):
                runner.stop_qemu(args, 22, process, require_clean=True)
        with self.assertRaises(subprocess.TimeoutExpired):
            with cache.locked(): cache.obtain(prepare)
        process.terminate.assert_called_once()
        self.assertFalse(cache.entry.exists())


class ResourcePinningTest(unittest.TestCase):
    def args(self, **changes):
        values = dict(host_cpus=None, host_numa_node=None, preallocate_memory=False,
                      lock_memory=False, cpus=2, memory=128, forward_sidecar_port=0)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_cpu_validation_rejects_empty_disallowed_and_undersized_sets(self):
        import run_qemu_player as runner
        with patch.object(os, 'sched_getaffinity', return_value={2, 3, 4}):
            for value in ('', '2-1', '2,,3', '2,9', '2'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    runner.validate_host_resources(self.args(host_cpus=value))
            self.assertEqual(runner.validate_host_resources(self.args(host_cpus='2-3')), [2, 3])

    def test_numa_binding_rejects_mismatched_cpus_and_disallowed_memory_node(self):
        import run_qemu_player as runner
        with patch.object(os, 'sched_getaffinity', return_value={2, 3}), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'read_text', side_effect=['4-7', 'Mems_allowed_list: 1\n']):
            with self.assertRaisesRegex(ValueError, 'must belong'):
                runner.validate_host_resources(self.args(host_cpus='2-3', host_numa_node=0))
            with self.assertRaisesRegex(ValueError, 'allowed memory nodes'):
                runner.validate_host_resources(self.args(host_numa_node=0))

    def test_lock_setup_raises_limit_only_in_child_and_reports_failure(self):
        import run_qemu_player as runner
        with patch.object(runner.resource, 'setrlimit') as limits:
            runner.qemu_child_setup(None, True)()
            limits.assert_called_once_with(runner.resource.RLIMIT_MEMLOCK, (-1, -1))
        with patch.object(runner.subprocess, 'Popen', side_effect=subprocess.SubprocessError('child failed')):
            with self.assertRaisesRegex(RuntimeError, 'RLIMIT_MEMLOCK'):
                runner.boot_qemu(self.args(lock_memory=True), Path('overlay'), Path('serial'), 22)

    def test_failed_vcpu_pinning_terminates_qemu(self):
        import run_qemu_player as runner
        process = Mock()
        with patch.object(runner, 'validate_host_resources', return_value=[2, 3]), \
             patch.object(runner.subprocess, 'Popen', return_value=process), \
             patch.object(runner, 'pin_vcpu_threads', side_effect=RuntimeError('pin failed')):
            with self.assertRaisesRegex(RuntimeError, 'pin failed'):
                runner.boot_qemu(self.args(host_cpus='2-3'), Path('overlay'), Path('serial'), 22)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)

    def test_vcpu_pinning_enables_qemu_thread_names(self):
        import run_qemu_player as runner
        with patch.object(runner, 'validate_host_resources', return_value=[2, 3]), \
             patch.object(runner.subprocess, 'Popen') as popen, \
             patch.object(runner, 'pin_vcpu_threads'):
            runner.boot_qemu(self.args(host_cpus='2-3'), Path('overlay'), Path('serial'), 22)
        command = popen.call_args.args[0]
        self.assertIn('debug-threads=on', command[command.index('-name') + 1])

    def test_vcpu_discovery_uses_boot_timeout_during_preallocation(self):
        import run_qemu_player as runner
        process = Mock()
        for extra, expected in (({}, 180), ({'boot_timeout': 240}, 240)):
            with self.subTest(extra=extra), \
                 patch.object(runner, 'validate_host_resources', return_value=[2, 3]), \
                 patch.object(runner.subprocess, 'Popen', return_value=process), \
                 patch.object(runner, 'pin_vcpu_threads') as pin:
                runner.boot_qemu(self.args(host_cpus='2-3', preallocate_memory=True, **extra),
                                 Path('overlay'), Path('serial'), 22)
            pin.assert_called_once_with(process, [2, 3], 2, timeout=expected)

    def test_interrupted_vcpu_pinning_terminates_qemu_before_return(self):
        import run_qemu_player as runner
        process = Mock()
        with patch.object(runner, 'validate_host_resources', return_value=[2, 3]), \
             patch.object(runner.subprocess, 'Popen', return_value=process), \
             patch.object(runner, 'pin_vcpu_threads', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.boot_qemu(self.args(host_cpus='2-3'), Path('overlay'), Path('serial'), 22)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)

    def test_memory_backend_and_lock_are_opt_in(self):
        import run_qemu_player as runner
        process = Mock()
        with patch.object(runner.subprocess, 'Popen', return_value=process) as popen:
            runner.boot_qemu(self.args(), Path('overlay'), Path('serial'), 22)
            default = popen.call_args
            self.assertNotIn('-object', default.args[0])
            self.assertNotIn('preexec_fn', default.kwargs)
        args = self.args(host_numa_node=0, preallocate_memory=True, lock_memory=True)
        with patch.object(runner, 'validate_host_resources', return_value=None), \
             patch.object(runner.subprocess, 'Popen', return_value=process) as popen:
            runner.boot_qemu(args, Path('overlay'), Path('serial'), 22)
            command = popen.call_args.args[0]
            backend = command[command.index('-object') + 1]
            for option in ('size=128M', 'prealloc=on', 'merge=off', 'host-nodes=0', 'policy=bind'):
                self.assertIn(option, backend)
            self.assertIn('mem-lock=on', command)

    def test_affinity_is_applied_before_child_exec(self):
        import run_qemu_player as runner
        cpus = sorted(os.sched_getaffinity(0))[:1]
        child = subprocess.run([sys.executable, '-c',
            'import os; print(*sorted(os.sched_getaffinity(0)))'],
            preexec_fn=runner.qemu_child_setup(cpus, False), capture_output=True, text=True, check=True)
        self.assertEqual(child.stdout.strip(), str(cpus[0]))

    def test_vcpu_discovery_fails_early_when_qemu_exits(self):
        import run_qemu_player as runner
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(runner.time, 'sleep') as sleep:
            process = Mock()
            process.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, 'QEMU exited'):
                runner.pin_vcpu_threads(process, [2, 3], 2,
                                        task_root=Path(folder), timeout=180)
            sleep.assert_not_called()

    def test_vcpu_threads_are_pinned_individually(self):
        import run_qemu_player as runner
        with tempfile.TemporaryDirectory() as folder:
            tasks = Path(folder)
            for tid, name in ((10, 'qemu-system-x86'), (11, 'CPU 0/KVM'), (12, 'CPU 1/KVM')):
                (tasks / str(tid)).mkdir()
                (tasks / str(tid) / 'comm').write_text(name)
            with patch.object(os, 'sched_setaffinity') as affinity:
                runner.pin_vcpu_threads(Mock(), [2, 3], 2, task_root=tasks)
            self.assertEqual(affinity.call_args_list[0].args, (11, {2}))
            self.assertEqual(affinity.call_args_list[1].args, (12, {3}))


class SparseArchiveTest(unittest.TestCase):
    def test_clean_sparse_checkout_does_not_reintroduce_excluded_model(self):
        from run_qemu_player import archive_repository
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'source'
            repository.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(repository), *args], check=True,
                                      capture_output=True, text=True)
            git('init')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repository / 'client.py').write_text('print(1)\n')
            (repository / 'demo.splat').write_bytes(b'fixture model')
            git('add', '.')
            git('commit', '-m', 'fixture')
            git('sparse-checkout', 'set', '--no-cone', '/*', '!/demo.splat')
            self.assertEqual(git('status', '--porcelain').stdout, '')
            archive = root / 'source.tar.gz'
            archive_repository(repository, archive)
            with tarfile.open(archive) as packed:
                self.assertIn('client.py', packed.getnames())
                self.assertNotIn('demo.splat', packed.getnames())


if __name__ == '__main__': unittest.main()
