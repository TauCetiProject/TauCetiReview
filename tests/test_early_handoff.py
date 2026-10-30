"""The selected engine can prove unchanged work before any expensive preparation."""
import contextlib
import copy
import importlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'runner'))
cli = importlib.import_module('runner.cli' if os.environ.get('TEST_INSTALLED_CLI') else 'cli')
import handoff
from casefile import carry_forward

HEAD = 'a' * 40
REPO = 'Owner/Repo'
REAL_RUN = subprocess.run


def board():
    return {'head_sha': HEAD, 'states': {r: 'green' for r in handoff.DEFAULT_RUBRICS},
            'replies_through': 10}


class ProbeTests(unittest.TestCase):
    def probe(self, *, value=None, replies=None, local=None, ledger_text=None, extra=(), env=None):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / 'board.json').write_text(json.dumps(board() if value is None else value))
            if replies != 'unavailable':
                (d / 'replies.json').write_text(json.dumps({} if replies is None else replies))
            if local is not None or ledger_text is not None:
                (d / 'ledger.json').write_text(ledger_text if ledger_text is not None else
                                              json.dumps({'days': {}, 'prs': {'1': local}}))
            before = {p.name: p.read_bytes() for p in d.iterdir()}
            cmd = [sys.executable, str(ROOT / 'runner/handoff.py'), '--repo', REPO, '--pr', '1',
                   '--head-sha', HEAD, '--store', str(d), '--board', str(d / 'board.json'),
                   '--replies', str(d / 'replies.json'), *extra]
            result = REAL_RUN(cmd, capture_output=True, text=True, timeout=5,
                              env={**os.environ, **(env or {})})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p.name: p.read_bytes() for p in d.iterdir()}, before)
            result = json.loads(result.stdout)
            self.assertEqual({k: result[k] for k in ('version', 'repo', 'pr', 'head_sha')},
                             {'version': 1, 'repo': REPO, 'pr': '1', 'head_sha': HEAD})
            return result['decision']

    def test_published_completion_and_adjudicated_contest_write_nothing(self):
        self.assertEqual(self.probe(), 'unchanged')
        self.assertEqual(self.probe(replies={'reuse': [{'id': 10}]}), 'unchanged')
        b = board(); b['states']['reuse'] = 'blocking_request'; b['states']['scope'] = 'stale'
        self.assertEqual(self.probe(value=b), 'unchanged')
        b['states']['reuse'] = 'blocking_block'; b['states']['scope'] = 'absent'
        self.assertEqual(self.probe(value=b), 'unchanged')

    def test_new_input_and_unavailable_or_malformed_evidence_continue(self):
        for changes in ({'head_sha': 'b' * 40}, {'mode': 'init'}, {'states': {}},
                        {'states': {'reuse': 'error'}}, {'states': []}):
            with self.subTest(changes=changes):
                self.assertEqual(self.probe(value={**board(), **changes}), 'continue')
        for replies in ('unavailable', [], {'reuse': [{'id': 11}]}, {'reuse': [{'id': 'bad'}]}):
            self.assertEqual(self.probe(replies=replies), 'continue')
        for text in ('{', '[]', '{"prs": []}'):
            self.assertEqual(self.probe(ledger_text=text), 'continue')

    def test_engine_rubric_configuration_and_selection(self):
        b = {'head_sha': HEAD, 'states': {'custom': 'green'}}
        self.assertEqual(self.probe(value=b), 'continue')
        self.assertEqual(self.probe(value=b, extra=['--rubrics', 'custom']), 'unchanged')
        self.assertEqual(self.probe(value=b, env={'TAUCETI_RUBRICS': '["custom"]'}), 'unchanged')
        self.assertEqual(self.probe(value={**board(), 'rubrics_version': 'old'}), 'unchanged')
        self.assertEqual(self.probe(env={'TAUCETI_CONF': 'https://invalid.test/project.toml',
                                        'TAUCETI_CONF_SNAPSHOT': '', 'TAUCETI_CONF_SOURCE': ''}), 'continue')

    def test_existing_project_snapshot_supplies_engine_rubrics_without_refresh(self):
        from tauceti_tools.scaffold import initial_configuration
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = pathlib.Path(tmp) / 'project.toml'
            text = initial_configuration(REPO, 'human') + '\n[review]\nrubrics=["custom"]\n'
            snapshot.write_text(text)
            env = {'TAUCETI_CONF': 'https://invalid.test/project.toml',
                   'TAUCETI_CONF_SOURCE': 'https://invalid.test/project.toml',
                   'TAUCETI_CONF_SNAPSHOT': str(snapshot), 'TAUCETI_RUBRICS': '["wrong"]'}
            self.assertEqual(self.probe(value={'head_sha': HEAD, 'states': {'custom': 'green'}},
                                        env=env), 'unchanged')
            self.assertEqual(snapshot.read_text(), text)

    def test_local_publication_obligations_and_carry_forward_are_not_skipped(self):
        for state in ({'pending_publication_head_sha': HEAD},
                      {'state': {'reuse': {'verdict': 'request_changes', 'reviewed_sha': HEAD}}},
                      {'state': {'reuse': {'verdict': 'block', 'reviewed_sha': HEAD,
                                          'pending_thread_run_id': 'run'}}}):
            self.assertEqual(self.probe(local=state), 'continue')
        old = {'state': {'reuse': {'verdict': 'request_changes', 'reviewed_sha': 'b' * 40}},
               'published_head_sha': 'b' * 40, 'scoreboard_comment_id': 7}
        self.assertEqual(self.probe(local=old), 'unchanged')
        # Without a diff, the early check must account for the late carry making
        # an old approval current and exposing the missing publication marker.
        local = {'scoreboard_comment_id': 7, 'state': {'reuse': {'verdict': 'approve',
                 'approved_sha': 'b' * 40, 'approved_digest': 'digest', 'approved_rubrics_version': 'v'}}}
        self.assertTrue(handoff.can_reuse_completed_review(board(), HEAD, ['reuse'], {}, local))
        self.assertEqual(self.probe(local=local), 'continue')
        carried = copy.deepcopy(local)
        self.assertEqual(carry_forward(carried['state'], HEAD, 'digest', 'v'), ['reuse'])
        self.assertFalse(handoff.can_reuse_completed_review(board(), HEAD, ['reuse'], {}, carried))

    def test_blind_marker_is_not_a_new_merge_barrier(self):
        # Current upstream deliberately removed the old prose-based merge gate.
        # Early reuse matches the late predicate even with legacy metadata.
        b = {**board(), 'blind': True}
        self.assertEqual(self.probe(value=b), 'unchanged')
        self.assertTrue(handoff.can_reuse_completed_review(b, HEAD, handoff.DEFAULT_RUBRICS, {}, {}))
        import merge_from_scoreboard
        comment = {'body': '<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 ' + json.dumps(b) + '-->'}
        selected = merge_from_scoreboard.latest_current_scoreboard([comment], HEAD)
        self.assertEqual(selected[1]['states'], b['states'])


class CLITests(unittest.TestCase):
    def invoke(self, *, flags=(), replies=None, value=None, collision=False, helper_error=None,
               old_engine=False, ledger_text=None, expect_cancel=False, auto_workspace=False, keep=False):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            d = pathlib.Path(tmp); work = d / 'work'; store = d / 'store'; store.mkdir()
            if ledger_text is not None:
                (store / 'ledger.json').write_text(ledger_text)
            value = board() if value is None else value
            meta = '<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 ' + json.dumps(value) + '-->'
            facts = [{'id': 7, 'body': meta}]
            events = []
            selected = ROOT
            if old_engine:
                selected = d / 'old'; (selected / 'runner').mkdir(parents=True)
                (selected / 'rubrics').mkdir(); (selected / 'runner/review.py').touch()

            def called(name, value):
                def fn(*args, **kwargs):
                    events.append(name); return value
                return fn

            def fake_run(cmd, **kwargs):
                name = pathlib.Path(str(cmd[1])).name if len(cmd) > 1 else ''
                if name == 'review.py':
                    events.append('engine')
                    (work / 'scoreboard.md').write_text('completed')
                    (work / 'post_plan.json').write_text(json.dumps({'review_changed': False}))
                elif cmd[:2] == ['git', 'clone']:
                    events.append('clone')
                    dest = pathlib.Path(cmd[-1]); dest.mkdir(parents=True, exist_ok=True)
                    if dest.name == 'code':
                        (dest / 'lake-manifest.json').write_text(json.dumps({'packages': [
                            {'name': 'mathlib', 'rev': 'c' * 40}]}))
                elif cmd[0] == 'git':
                    events.append('git')
                else:
                    raise AssertionError(('unexpected command', cmd))
                return types.SimpleNamespace(returncode=0, stdout='', stderr='')

            def sub(cmd, **kwargs):
                name = pathlib.Path(cmd[1]).name
                if name == 'handoff.py':
                    events.append('probe')
                    if helper_error:
                        if isinstance(helper_error, BaseException):
                            raise helper_error
                        return types.SimpleNamespace(returncode=0, stdout=helper_error)
                    return REAL_RUN(cmd, **kwargs)
                if name == 'pr_diff.py':
                    events.append('diff'); return types.SimpleNamespace(returncode=0)
                raise AssertionError(('unexpected subprocess', cmd))

            argv = ['tauceti-review', '1', '--repo', REPO, '--reviewer', 'claude', '--no-archive',
                    '--post', *([] if auto_workspace else ['--workdir', str(work)]),
                    *(['--keep'] if keep else []), '--store', str(store), '--submitted-by', 'test',
                    '--expect-head', HEAD, *flags]
            if auto_workspace:
                stack.enter_context(patch.object(cli.tempfile, 'mkdtemp', return_value=str(work)))
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(patch.dict(os.environ, {'TAUCETI_REVIEW_RESULT_FILE': str(d / 'result.json')}))
            stack.enter_context(patch.object(cli, 'need'))
            stack.enter_context(patch.object(cli, 'run', fake_run))
            stack.enter_context(patch.object(cli.subprocess, 'run', sub))
            stack.enter_context(patch.object(cli.shutil, 'which', return_value='/fake/cli'))
            stack.enter_context(patch.object(cli, 'resolve_repo_dir', return_value=selected))
            stack.enter_context(patch.object(cli, 'pr_ref_oids', called('head', (HEAD, 'b' * 40))))
            stack.enter_context(patch.object(cli, 'coordinate', called('claim', [] if collision else ['claude'])))
            stack.enter_context(patch.object(cli, 'fetch_thread_replies', called('replies', replies)))
            stack.enter_context(patch.object(cli, 'issue_comments', called('board', facts)))
            stack.enter_context(patch.object(cli, 'merge_base_sha', called('compare', 'b' * 40)))
            stack.enter_context(patch.object(cli, 'gh_json', called('pr-context', {})))
            stack.enter_context(patch.object(cli, 'rubrics_repo_sha', called('rubrics-sha', ('c' * 40, False))))
            stack.enter_context(patch.object(cli, 'rubrics_publication', called('rubrics-publication', (False, True))))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if expect_cancel:
                with self.assertRaises(KeyboardInterrupt): cli.main()
                self.assertNotIn('engine', events)
            else:
                result = cli.main()
                self.assertEqual(result, cli.COORDINATED_ELSEWHERE_EXIT if collision else 0)
                if not collision:
                    self.assertEqual(json.loads((d / 'result.json').read_text()),
                                     {'head_sha': HEAD, 'changed': False})
            if auto_workspace:
                self.assertEqual(work.exists(), keep)
            return events

    def test_completed_cli_omits_expensive_work(self):
        self.assertEqual(self.invoke(replies={}), ['head', 'claim', 'replies', 'board', 'probe'])

    def test_unchanged_owned_workspace_cleanup_and_keep(self):
        for keep in (False, True):
            events = self.invoke(replies={}, auto_workspace=True, keep=keep)
            self.assertNotIn('clone', events)

    def test_collision_omits_history_and_probe(self):
        self.assertEqual(self.invoke(collision=True), ['head', 'claim'])

    def test_continue_reuses_histories_and_preserves_mode_semantics(self):
        for kwargs in ({'replies': None}, {'replies': {'reuse': [{'id': 11}]}},
                       {'replies': {}, 'value': {**board(), 'head_sha': 'b' * 40}},
                       {'replies': {}, 'old_engine': True},
                       {'replies': {}, 'ledger_text': '{'}):
            events = self.invoke(**kwargs)
            self.assertIn('engine', events); self.assertEqual(events.count('replies'), 1)
            self.assertEqual(events.count('board'), 1)
            self.assertIn('clone', events); self.assertIn('rubrics-publication', events)
        for flags in (['--mode', 'manual'], ['--fresh']):
            events = self.invoke(replies={}, flags=flags)
            self.assertIn('engine', events); self.assertNotIn('probe', events)

    def test_nonzero_probe_and_failed_diagnostics_do_not_change_decision(self):
        expected = json.dumps({'version': 1, 'repo': REPO, 'pr': '1', 'head_sha': HEAD,
                               'decision': 'unchanged', 'future_optional_field': True})
        args = (ROOT, REPO, 1, HEAD, ROOT, ROOT / 'board', ROOT / 'replies', '')
        with patch.object(cli.subprocess, 'run', return_value=types.SimpleNamespace(
                returncode=1, stdout=expected)):
            self.assertFalse(cli.review_completed_before_preparation(*args))
        with patch.object(cli.subprocess, 'run', return_value=types.SimpleNamespace(
                returncode=0, stdout=expected)), patch.object(sys.stderr, 'write', side_effect=OSError()):
            self.assertTrue(cli.review_completed_before_preparation(*args))

    def test_hung_optional_child_is_killed_and_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp); (d / 'runner').mkdir()
            pid_file = d / 'pid'
            (d / 'runner/handoff.py').write_text(
                'import os,time,pathlib\npathlib.Path(' + repr(str(pid_file)) +
                ').write_text(str(os.getpid()))\ntime.sleep(30)\n')
            self.assertFalse(cli.review_completed_before_preparation(
                d, REPO, 1, HEAD, d, d / 'board', d / 'replies', ''))
            if pid_file.exists():  # A loaded runner may kill the child before its first instruction.
                pid = int(pid_file.read_text())
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_bad_optional_probe_cannot_block_work_or_swallow_cancellation(self):
        for failure in ('garbage', '{}', '[' * 2000 + ']' * 2000,
                        json.dumps({'version': True, 'repo': REPO, 'pr': '1', 'head_sha': HEAD, 'decision': 'unchanged'}), json.dumps({'version': 1, 'repo': REPO, 'pr': '1',
                        'head_sha': 'wrong', 'decision': 'unchanged'}), OSError('no helper'),
                        subprocess.TimeoutExpired('helper', 2)):
            self.assertIn('engine', self.invoke(replies={}, helper_error=failure))
        self.invoke(replies={}, helper_error=KeyboardInterrupt(), expect_cancel=True)


if __name__ == '__main__':
    if os.environ.get('TEST_INSTALLED_CLI'):
        assert str(cli.__file__).startswith(os.environ['TEST_INSTALLED_CLI']), cli.__file__
    unittest.main()
