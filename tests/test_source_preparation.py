#!/usr/bin/env python3
"""Exact source/diff reuse and pre-inference recovery without models or network."""
import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'runner'))
import cli
import pr_diff
from test_pr_diff import _fixture, _diff, _git


def test_same_fetch_supplies_exact_patch_paths_and_source():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        url, mb, head, tip = _fixture(root)
        src = root / 'src'
        _git(src, 'checkout', '-q', head)
        (src / 'linked').symlink_to('TauCeti/F000.lean')
        _git(src, 'add', 'linked')
        _git(src, 'update-index', '--add', '--cacheinfo', '160000,' + mb + ',submodule')
        _git(src, 'commit', '-qm', 'source metadata')
        head = _git(src, 'rev-parse', 'HEAD')
        work = root / 'work'; work.mkdir()
        real = pr_diff.git_diff
        calls = []
        def local(_remote, *args, **kwargs):
            calls.append(kwargs)
            return real(url, *args, **kwargs)
        with patch.object(pr_diff, 'git_diff', local):
            git = pr_diff._git
            def offline(directory, args, *a, **kw):
                if args[0] == 'fetch':
                    assert a[1] is None and kw['config'] == ('http.lowSpeedLimit=1', 'http.lowSpeedTime=300')
                    raise pr_diff.PreparationUnavailable('connection reset')
                return git(directory, args, *a, **kw)
            with patch.object(pr_diff, '_git', offline):
                try:
                    cli.prepare_review_source('o/r', head, mb, work)
                except pr_diff.PreparationUnavailable:
                    pass
                else:
                    raise AssertionError('offline preparation succeeded')
            assert not (work / 'code').exists() and not (work / 'diff.txt.partial').exists()
            cli.prepare_review_source('o/r', head, mb, work)
        assert (work / 'diff.txt').read_bytes() == _diff(url, mb, head)[0]
        assert (work / 'code/TauCeti/F319.lean').read_text().endswith('by trivial\n')
        assert not (work / 'code/MainOnly.lean').exists()
        assert not (work / 'code/TauCeti/Gone.lean').exists()
        assert (work / 'code/TauCeti/Renamed.lean').is_file()
        assert not list((work / 'code').glob('**/.git'))
        assert (work / 'code/linked').is_symlink()
        assert os.readlink(work / 'code/linked') == 'TauCeti/F000.lean'
        assert (work / 'code/script.sh').stat().st_mode & 0o111
        assert (work / 'code/logo.png').read_bytes() == (src / 'logo.png').read_bytes()
        assert (work / 'code/submodule').is_dir()
        assert b'submodule\0' in (work / 'paths.z').read_bytes()
        assert len(calls) == 2 and calls[-1]['limit'] is None and calls[-1]['fetch_limit'] is None
        assert b'outside\nnewline.txt\0' in (work / 'paths.z').read_bytes()


def test_preparation_outage_retries_without_models_or_stale_workspace():
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
        root = pathlib.Path(d); work = root / 'work'; store = root / 'store'; store.mkdir()
        (store / 'outbox').mkdir(); (store / 'outbox/receipt').write_text('keep')
        marker_releases = []
        stack.enter_context(patch.object(sys, 'argv', ['tauceti-review', '7', '--reviewer', 'claude',
            '--no-archive', '--no-mathlib', '--submitted-by', 'tester', '--store', str(store)]))
        stack.enter_context(patch.dict(os.environ, {'TAUCETI_REVIEW_REF': '', 'TAUCETI_CLAUDE_MODEL': ''}))
        stack.enter_context(patch.object(cli.tempfile, 'mkdtemp', return_value=str(work)))
        stack.enter_context(patch.object(cli, 'need'))
        stack.enter_context(patch.object(cli.shutil, 'which', return_value='/fake/claude'))
        stack.enter_context(patch.object(cli, 'resolve_repo_dir', return_value=root))
        stack.enter_context(patch.object(cli, 'pr_ref_oids', return_value=('a' * 40, 'b' * 40)))
        stack.enter_context(patch.object(cli, 'merge_base_sha', side_effect=pr_diff.PreparationUnavailable('HTTP 503')))
        stack.enter_context(patch.object(cli, 'fetch_thread_replies', return_value=None))
        stack.enter_context(patch.object(cli, 'issue_comments', return_value=[]))
        stack.enter_context(patch.object(cli, 'run', side_effect=AssertionError('launched a model or clone')))
        stack.enter_context(patch.object(cli, '_ACTIVE_GIT_CLAIMS', [types.SimpleNamespace(
            release=lambda: marker_releases.append(True))]))
        with contextlib.redirect_stderr(io.StringIO()):
            assert cli.main() == 83
        assert not work.exists() and marker_releases == [True]
        assert (store / 'outbox/receipt').read_text() == 'keep'
        with patch.object(cli, 'merge_base_sha', side_effect=RuntimeError('HTTP 404')), patch.object(
                cli, 'pr_ref_oids', side_effect=[('a' * 40, 'b' * 40), ('d' * 40, 'b' * 40)]):
            with contextlib.redirect_stderr(io.StringIO()):
                assert cli.main() == cli.HEAD_CHANGED_EXIT
        assert not work.exists()
        for message, expected in [('not our ref ' + 'a' * 40, 83),
                                  ('not our ref ' + 'c' * 40, 83), ('HTTP 404', 83)]:
            with patch.object(cli, 'merge_base_sha', return_value='c' * 40), patch.object(
                    cli, 'prepare_review_source', side_effect=RuntimeError(message)):
                with contextlib.redirect_stderr(io.StringIO()):
                    assert cli.main() == expected, message
            assert not work.exists()
        # The next ordinary attempt starts clean and reaches preparation again.
        with patch.object(cli, 'merge_base_sha', return_value='c' * 40), patch.object(
            cli, 'prepare_review_source', side_effect=RuntimeError('invalid comparison')) as prepare:
            with contextlib.redirect_stderr(io.StringIO()):
                assert cli.main() == 83
            prepare.assert_called_once()
        assert not work.exists() and (store / 'outbox/receipt').exists()


def test_relative_workspace_stages_source_at_the_requested_path():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        url, mb, head, _ = _fixture(root)
        original = pr_diff.git_diff
        cwd = pathlib.Path.cwd()
        try:
            os.chdir(root)
            work = pathlib.Path('relative-work'); work.mkdir()
            with patch.object(pr_diff, 'git_diff', lambda _url, *a, **kw: original(url, *a, **kw)):
                cli.prepare_review_source('o/r', head, mb, work)
            assert (work / 'code/TauCeti/F319.lean').is_file()
            assert (work / 'diff.txt').is_file() and not (work / 'code/.git').exists()
        finally:
            os.chdir(cwd)


def _fresh_archive_failure(post_status):
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
        root = pathlib.Path(d); work = root / 'work'
        argv = ['tauceti-review', '7', '--reviewer', 'claude', '--submitted-by', 'tester',
                '--fresh', '--post', '--no-sync', '--no-mathlib', '--no-coordinate']
        if post_status == 0:
            argv.remove('--no-sync')
        stack.enter_context(patch.object(sys, 'argv', argv))
        stack.enter_context(patch.dict(os.environ, {'TAUCETI_REVIEW_REF': '', 'TAUCETI_CLAUDE_MODEL': ''}))
        stack.enter_context(patch.object(cli.tempfile, 'mkdtemp', return_value=str(work)))
        stack.enter_context(patch.object(cli, 'need'))
        stack.enter_context(patch.object(cli.shutil, 'which', return_value='/fake/claude'))
        stack.enter_context(patch.object(cli, 'resolve_repo_dir', return_value=root))
        stack.enter_context(patch.object(cli, 'pr_ref_oids', return_value=('a' * 40, 'b' * 40)))
        stack.enter_context(patch.object(cli, 'merge_base_sha', return_value='c' * 40))
        stack.enter_context(patch.object(cli, 'prepare_review_source'))
        stack.enter_context(patch.object(cli, 'fetch_thread_replies', return_value=None))
        stack.enter_context(patch.object(cli, 'issue_comments', return_value=[]))
        stack.enter_context(patch.object(cli, 'gh_json', return_value={}))
        stack.enter_context(patch.object(cli, 'rubrics_repo_sha', return_value=('d' * 40, False)))
        stack.enter_context(patch.object(cli, 'rubrics_publication', return_value=(False, True)))
        stack.enter_context(patch.object(cli, 'cli_sha', return_value=('e' * 40, False)))
        def run(cmd, **kwargs):
            if len(cmd) > 1 and pathlib.Path(cmd[1]).name == 'review.py':
                (work / 'scoreboard.md').write_text('reviewed')
                (work / 'post_plan.json').write_text(json.dumps(dict(head_sha='a' * 40, scoreboard_body=str(work / 'scoreboard.md'), threads=[])))
                (work / 'store/outbox').mkdir(parents=True)
                (work / 'store/outbox/receipt').write_text('keep paid evidence')
            elif len(cmd) > 1 and pathlib.Path(cmd[1]).name == 'archive.py':
                return types.SimpleNamespace(returncode=1)
            return types.SimpleNamespace(returncode=0, stdout='test-token', stderr='')
        stack.enter_context(patch.object(cli, 'run', side_effect=run))
        stack.enter_context(patch.object(cli.publication.post, 'transport_for', return_value=types.SimpleNamespace(identity=lambda: 'tester')))
        stack.enter_context(patch.object(cli.publication, 'publish', return_value=post_status))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            assert cli.main() in ((None, 0) if post_status == 0 else (85,))
        assert json.loads((work / 'publication.json').read_text())['state'] == 'prepared'
        assert (work / 'store/outbox/receipt').read_text() == 'keep paid evidence'


def test_post_failure_retains_fresh_archive_receipts():
    _fresh_archive_failure(85)


def test_archive_failure_retains_fresh_receipts_after_successful_post():
    _fresh_archive_failure(0)


def test_transport_classification_keeps_deterministic_failures_distinct():
    for error, transient in [('HTTP 503', True), ('HTTP 429', True), ('HTTP 401', True),
            ('Could not resolve host', True), ('RPC failed; early EOF', True),
            ('error connecting to api.github.com', True), ('dial tcp: i/o timeout', True),
            ('unexpected EOF', True), ('context deadline exceeded', True), ('Operation too slow', True),
            ('HTTP 404', False), ('HTTP 422', False), ('not our ref abc', False),
            ('malformed response', False)]:
        with patch.object(pr_diff.subprocess, 'run', return_value=types.SimpleNamespace(
                returncode=1, stdout='', stderr=error)):
            try:
                pr_diff._gh_api('/test', '.')
            except RuntimeError as result:
                assert isinstance(result, pr_diff.PreparationUnavailable) == transient, error
            else:
                raise AssertionError('failure became success')


def test_local_timeout_is_not_a_transport_wait_and_disk_errors_are_host_failures():
    with tempfile.TemporaryDirectory() as d:
        try:
            pr_diff._git(d, ['-c', 'alias.wait=!sleep 90', 'wait'], pr_diff._git_env(), .1)
        except RuntimeError as error:
            assert not isinstance(error, pr_diff.PreparationUnavailable)
        else:
            raise AssertionError('local timeout succeeded')
        try:
            pr_diff._git(d, ['-c', 'alias.full=!echo "No space left on device" >&2; exit 1', 'full'],
                         pr_diff._git_env(), 5)
        except pr_diff.PreparationUnavailable:
            pass
        else:
            raise AssertionError('host disk failure was not recognized')


def test_signal_cleans_git_group_and_temporary_repository():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d); pidfile = root / 'child.pid'
        program = '''import sys,signal,tempfile
sys.path.insert(0,sys.argv[1])
import cli,pr_diff
cli._install_claim_cleanup()
with tempfile.TemporaryDirectory(dir=sys.argv[2],prefix='objects-') as repo:
 pr_diff._git(repo,['-c','alias.wait=!echo $$ > '+sys.argv[3]+'; sleep 90','wait'],pr_diff._git_env(),120)
'''
        process = subprocess.Popen([sys.executable, '-c', program, str(pathlib.Path(cli.__file__).parent), d, str(pidfile)])
        try:
            deadline = time.monotonic() + 10
            while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert pidfile.exists()
            child = int(pidfile.read_text())
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=10) == 143
            assert not list(root.glob('objects-*'))
            # A killed shell may remain briefly as an orphan zombie, but must not run.
            status = pathlib.Path('/proc', str(child), 'stat')
            assert not status.exists() or status.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
        finally:
            if process.poll() is None:
                process.kill(); process.wait()


def test_owner_sigkill_does_not_leave_git_running():
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d); pidfile = root / 'child.pid'
        program = '''import sys
sys.path.insert(0,sys.argv[1])
import pr_diff
pr_diff._git(sys.argv[2],['-c','alias.wait=!echo $$ > '+sys.argv[3]+'; sleep 90','wait'],pr_diff._git_env(),120)
'''
        process = subprocess.Popen([sys.executable, '-c', program, str(pathlib.Path(cli.__file__).parent), d, str(pidfile)])
        try:
            deadline = time.monotonic() + 10
            while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert pidfile.exists()
            child = int(pidfile.read_text()); process.kill(); process.wait(timeout=10)
            deadline = time.monotonic() + 5
            status = pathlib.Path('/proc', str(child), 'stat')
            while status.exists() and status.read_text().rsplit(')', 1)[1].split()[0] != 'Z' and time.monotonic() < deadline:
                time.sleep(.02)
            assert not status.exists() or status.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
        finally:
            if process.poll() is None:
                process.kill(); process.wait()


if __name__ == '__main__':
    for name, test in sorted(list(globals().items())):
        if name.startswith('test_'):
            test(); print('ok', name)
