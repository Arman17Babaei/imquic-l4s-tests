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


if __name__ == '__main__': unittest.main()
