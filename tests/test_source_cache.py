"""Source reuse, private writes and deterministic acquisition/collection crash races."""
import contextlib
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner import cli, source_cache as cache, source_tree


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'; self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'Mathlib').mkdir()
        (self.repo / 'Mathlib.lean').write_text('import Mathlib.A\n')
        (self.repo / 'Mathlib/A.lean').write_text('-- first\n')
        (self.repo / 'script').write_text('#!/bin/sh\n'); (self.repo / 'script').chmod(0o755)
        (self.repo / 'link').symlink_to('Mathlib/A.lean')
        self.first = self.commit()
        (self.repo / 'Mathlib/A.lean').write_text('-- second\n')
        self.second = self.commit()
        self.directory = self.base / 'shared'
        self.directory.mkdir()
        self.root = self.directory / 'exact-sources-v1'

    def commit(self):
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
        return git(self.repo, 'rev-parse', 'HEAD')

    def publish(self, pin):
        git(self.repo, 'checkout', '-q', pin)
        cache.publish(self.directory, pin, self.repo)
        self.assertTrue((self.root / pin / 'source/Mathlib.lean').is_file())

    def test_exact_manifest_consumer_fetches_once_then_uses_shared_source(self):
        manifest = self.base / 'lake-manifest.json'
        manifest.write_text(json.dumps(dict(packages=[dict(name='mathlib', rev=self.first)])))
        real_run = cli.run
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return real_run([self.repo.as_uri() if arg == cache.URL else arg for arg in command], **kwargs)
        with patch.object(cli, 'run', run):
            self.assertTrue(cli.prepare_mathlib_source(manifest, self.base / 'one', self.directory))
            before = len(calls)
            self.assertTrue(cli.prepare_mathlib_source(manifest, self.base / 'two', self.directory))
            self.assertEqual(len(calls), before)
            manifest.write_text(json.dumps(dict(packages=[dict(name='mathlib', rev=self.second)])))
            self.assertTrue(cli.prepare_mathlib_source(manifest, self.base / 'three', self.directory))
        self.assertEqual((self.base / 'two/Mathlib/A.lean').read_text(), '-- first\n')
        self.assertEqual((self.base / 'three/Mathlib/A.lean').read_text(), '-- second\n')
        self.assertFalse((self.base / 'two/.git').exists())
        (self.base / 'two/Mathlib/A.lean').write_text('private change')
        self.assertEqual((self.root / self.first / 'source/Mathlib/A.lean').read_text(), '-- first\n')

    def test_cached_copy_failure_retries_privately_and_preserves_existing_workdir(self):
        self.publish(self.first)
        manifest = self.base / 'lake-manifest.json'
        manifest.write_text(json.dumps(dict(packages=[dict(name='mathlib', rev=self.first)])))
        real_run = cli.run
        def run(command, **kwargs):
            return real_run([self.repo.as_uri() if arg == cache.URL else arg for arg in command], **kwargs)
        real_file = source_tree.copy_file
        failed = []
        def copy(src, dst):
            if not failed and Path(src).name == 'Mathlib.lean':
                failed.append(True)
                raise OSError(errno.EIO, 'fixture')
            return real_file(src, dst)
        def checked_run(command, **kwargs):
            if 'fetch' in command:
                with cache._lock(self.root, self.first, write=True):
                    pass  # No shared cache lease survives into the fallback fetch.
            return run(command, **kwargs)
        with patch.object(cli, 'run', checked_run), patch.object(source_tree, 'copy_file', copy), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(cli.prepare_mathlib_source(manifest, self.base / 'retry', self.directory))
        self.assertFalse((self.base / 'retry/partial').exists())
        self.assertEqual((self.base / 'retry/Mathlib/A.lean').read_text(), '-- first\n')
        # An explicitly retained caller workspace uses its old in-place Git preparation.
        (self.base / 'retry/user-note').write_text('keep')
        with patch.object(cli, 'run', run), patch.object(source_tree, 'copy_tree', side_effect=AssertionError('copied existing work')):
            with self.assertRaises(SystemExit):
                cli.prepare_mathlib_source(manifest, self.base / 'retry', self.directory)
        self.assertEqual((self.base / 'retry/user-note').read_text(), 'keep')

    def test_partial_copy_failure_restores_directory_permissions(self):
        source = self.base / 'readonly'; source.mkdir()
        (source / 'done').mkdir(); (source / 'done/file').write_text('one')
        (source / 'later').write_text('two'); (source / 'done').chmod(0o555)
        (source / 'done/file').chmod(0o444)
        real_file = source_tree.copy_file
        def copy(src, dst):
            if Path(src).name == 'later':
                raise OSError(errno.EIO, 'fixture')
            return real_file(src, dst)
        with patch.object(source_tree, 'copy_file', copy), self.assertRaises(OSError):
            source_tree.copy_tree(source, self.base / 'partial')
        self.assertTrue((self.base / 'partial/done').stat().st_mode & 0o200)
        self.assertTrue((source / 'done').stat().st_mode & 0o222 == 0)

    def test_two_versions_and_gc_do_not_deadlock_or_remove_readers(self):
        self.publish(self.first); self.publish(self.second)
        with cache.borrowed(self.directory, self.first) as first, cache.borrowed(self.directory, self.second) as second:
            report = cache.collect(self.directory, now=time.time() + 2 * cache.PUBLICATION_AGE)
            self.assertEqual(len(report['protected']), 2)
            self.assertTrue(first.is_dir() and second.is_dir())
        report = cache.collect(self.directory, now=time.time() + 2 * cache.PUBLICATION_AGE)
        self.assertEqual(len(report['removed']), 2)
        self.assertFalse(report['errors'])
        self.assertTrue((self.root / 'locks' / self.first).exists())

    def test_busy_version_falls_back_without_blocking_other_pin(self):
        self.publish(self.first); self.publish(self.second)
        with cache._lock(self.root, self.first, write=True):
            with cache.borrowed(self.directory, self.first) as source:
                self.assertIsNone(source)
            with cache.borrowed(self.directory, self.second) as source:
                self.assertTrue(source.is_dir())

    def test_detached_gc_never_exposes_partial_source(self):
        self.publish(self.first); self.publish(self.second)
        real_remove = cache._remove
        def remove(path):
            with cache.borrowed(self.directory, self.first) as source:
                self.assertIsNone(source)
            with cache.borrowed(self.directory, self.second) as source:
                self.assertTrue(source.is_dir())
            real_remove(path)
        os.utime(self.root / self.first, (0, 0))
        with patch.object(cache, '_remove', remove):
            report = cache.collect(self.directory)
        self.assertFalse(report['errors'])
        self.assertFalse((self.root / self.first).exists())

    def test_crash_debris_needs_no_surviving_payload_marker_and_dry_run_does_not_mutate(self):
        self.publish(self.first)
        stage = self.root / (self.second + '.staging')
        stage.mkdir()  # Publisher killed immediately after mkdir, before readiness write.
        with cache._lock(self.root, self.second, write=True):
            pass
        trash = self.root / (self.first + '.trash')
        (self.root / self.first).rename(trash)
        trash.chmod(0o700)
        (trash / 'ready.json').unlink()  # Collector killed partway through deletion.
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        report = cache.collect(self.directory, dry=True)
        self.assertEqual(len(report['removed']), 2)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        report = cache.collect(self.directory)
        self.assertEqual(len(report['removed']), 2)
        self.assertFalse(report['errors'])
        self.assertFalse(stage.exists() or trash.exists())

    def test_failed_optional_publication_preserves_private_work(self):
        with patch.object(cache, 'copy_tree', side_effect=OSError(errno.ENOSPC, 'fixture')), contextlib.redirect_stderr(io.StringIO()):
            cache.publish(self.directory, self.second, self.repo)
        self.assertFalse((self.root / self.second).exists())
        self.assertFalse(list(self.root.glob('*.staging')))
        self.assertEqual((self.repo / 'Mathlib/A.lean').read_text(), '-- second\n')
        # Wrong/dirty inputs and an unknown existing root are never adopted.
        with contextlib.redirect_stderr(io.StringIO()):
            cache.publish(self.directory, self.first, self.repo)
        self.assertFalse((self.root / self.first).exists())
        unknown = self.base / 'unknown/exact-sources-v1'; unknown.mkdir(parents=True)
        (unknown / 'user-file').write_text('keep')
        with contextlib.redirect_stderr(io.StringIO()):
            cache.publish(unknown.parent, self.second, self.repo)
        self.assertEqual((unknown / 'user-file').read_text(), 'keep')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux process death fixture')
    def test_killed_reader_releases_lease_and_killed_publisher_stage_is_collected(self):
        self.publish(self.first)
        marker = self.base / 'entered'
        code = '''import sys,time
from pathlib import Path
from runner import source_cache as c
with c.borrowed(sys.argv[1], sys.argv[2]) as source:
 assert source is not None
 Path(sys.argv[3]).touch()
 time.sleep(90)
'''
        process = subprocess.Popen([sys.executable, '-c', code, str(self.directory), self.first, str(marker)], cwd=Path(__file__).resolve().parents[1])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists())
        self.assertEqual(len(cache.collect(self.directory, now=time.time() + 2 * cache.PUBLICATION_AGE)['protected']), 1)
        process.kill(); process.wait(timeout=10)
        self.assertEqual(len(cache.collect(self.directory, now=time.time() + 2 * cache.PUBLICATION_AGE)['removed']), 1)
        # Kill in publication after the owned marker is durable enough to recover.
        marker.unlink()
        git(self.repo, 'checkout', '-q', self.second)
        code = '''import sys,time
from pathlib import Path
from runner import source_cache as c
def copy(*a, **kw):
 Path(sys.argv[4]).touch()
 time.sleep(90)
c.copy_tree=copy
c.publish(sys.argv[1],sys.argv[2],Path(sys.argv[3]))
'''
        process = subprocess.Popen([sys.executable, '-c', code, str(self.directory), self.second, str(self.repo), str(marker)], cwd=Path(__file__).resolve().parents[1])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists())
        process.kill(); process.wait(timeout=10)
        report = cache.collect(self.directory)
        self.assertEqual(len(report['removed']), 1)
        self.assertFalse(list(self.root.glob('*.staging')))
        self.publish(self.second)

    def test_empty_initialization_is_recoverable_and_relative_cache_is_optional(self):
        self.root.mkdir()
        self.publish(self.second)
        with contextlib.redirect_stderr(io.StringIO()):
            with cache.borrowed('relative/path', self.second) as source:
                self.assertIsNone(source)
        self.assertFalse((Path.cwd() / 'relative/path').exists())

    def test_unsupported_platform_and_unsafe_root_use_private_preparation(self):
        with patch.object(cache, 'fcntl', None):
            cache.publish(self.directory, self.second, self.repo)
            self.assertEqual(cache.collect(self.directory)['errors'], [])
            with cache.borrowed(self.directory, self.second) as source:
                self.assertIsNone(source)
        self.assertFalse(self.root.exists())
        self.root.mkdir(); self.root.chmod(0o777)
        with contextlib.redirect_stderr(io.StringIO()):
            cache.publish(self.directory, self.second, self.repo)
        self.assertFalse((self.root / self.second).exists())
        self.root.chmod(0o700)

    def test_partial_copy_cleanup_outage_is_pre_inference_preparation_wait(self):
        self.publish(self.first)
        manifest = self.base / 'lake-manifest.json'
        manifest.write_text(json.dumps(dict(packages=[dict(name='mathlib', rev=self.first)])))
        def fail(src, dst):
            dst.mkdir()
            raise OSError(errno.EIO, 'copy fixture')
        with patch.object(source_tree, 'copy_tree', fail), patch.object(cli.shutil, 'rmtree', side_effect=OSError(errno.EIO, 'cleanup fixture')), patch.object(cli, 'run', side_effect=AssertionError('fetch launched over partial input')), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(cli.repository_diff.PreparationUnavailable):
                cli.prepare_mathlib_source(manifest, self.base / 'incomplete', self.directory)

    def test_copy_is_private_writable_and_has_byte_copy_fallback(self):
        self.publish(self.first)
        source = self.root / self.first / 'source'
        with patch.object(fcntl, 'ioctl', side_effect=OSError(errno.EOPNOTSUPP, 'fixture')):
            source_tree.copy_tree(source, self.base / 'copy')
        copy = self.base / 'copy'
        self.assertNotEqual((copy / 'Mathlib/A.lean').stat().st_ino, (source / 'Mathlib/A.lean').stat().st_ino)
        (copy / 'Mathlib/A.lean').write_text('private')
        (copy / 'Mathlib/new.lean').write_text('new')
        self.assertEqual((source / 'Mathlib/A.lean').read_text(), '-- first\n')
        self.assertTrue((copy / 'script').stat().st_mode & 0o100)
        self.assertTrue((copy / 'link').is_symlink())
        self.assertFalse((copy / '.git').exists())


if __name__ == '__main__':
    unittest.main()
