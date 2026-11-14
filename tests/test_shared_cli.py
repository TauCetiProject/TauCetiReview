"""Both sources use the real engine, shared workspace and publication lifecycle."""
import contextlib
import io
import json
import os
import pathlib
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from runner import cli
from test_shared_publication import HEAD, workflow


class SharedCLI(unittest.TestCase):
    def test_configured_cli_starts_for_both_content_forges(self):
        for kind in ('github', 'forgejo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                configuration = pathlib.Path(directory) / 'project.toml'
                content = ('"Owner/Project"' if kind == 'github' else '{forge="native",name="Owner/Project"}')
                configuration.write_text('schema_version=2\n[forges.native]\nkind="forgejo"\n'
                    'url="https://git.example.org"\n[repositories]\ncontent='+content+'\nroadmap="Owner/Roadmap"\n')
                env = {k:v for k,v in os.environ.items() if not k.startswith(('TAUCETI_', 'GH_'))}
                for selection in ('flag','environment'):
                    command=[sys.executable,str(pathlib.Path(cli.__file__).resolve()),'--help']
                    child = dict(env)
                    if selection == 'flag': command += ['--conf',str(configuration)]
                    else: child['TAUCETI_CONF']=str(configuration)
                    result=subprocess.run(command,env=child,cwd=directory,capture_output=True,text=True,timeout=30)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertIn('--conf',result.stdout)
                if kind == 'forgejo':
                    refused = subprocess.run([sys.executable, str(pathlib.Path(cli.__file__).resolve()),
                        '7', '--conf', str(configuration)], env=env, cwd=directory,
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(refused.returncode, 1)
                    self.assertIn('TAUCETI_FORGEJO_REPOSITORY_ID', refused.stderr)
                    self.assertNotIn('Traceback', refused.stderr)

    def test_post_inference_errors_cannot_be_classified_as_preparation_or_review_errors(self):
        from tauceti_tools.forge import Incomplete

        for inference, expected in ((False, 83), (True, 85)):
            for error in (Incomplete('source unavailable'), OSError('storage unavailable'), ValueError('invalid record')):
                with self.subTest(inference=inference, error=type(error).__name__):
                    def fail(cleanup, leases, phase):
                        phase.update(preparing=True, inference=inference)
                        raise error
                    with patch.object(cli, '_main', side_effect=fail), contextlib.redirect_stderr(io.StringIO()):
                        assert cli.main() == expected

    def test_engine_handoff_unchanged_retry_and_context_for_both_sources(self):
        for kind in ('github', 'forgejo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                root = pathlib.Path(directory)
                w = workflow(kind, root, stack)
                work = w.receipt.parent
                w.receipt.unlink()
                (work / 'post_plan.json').unlink()
                engine = root / 'engine'; engine.mkdir()
                (engine / 'runner').symlink_to(pathlib.Path(cli.__file__).resolve().parent, target_is_directory=True)
                (engine / 'rubrics').mkdir()
                (engine / 'rubrics/_common.md').write_text('Return a structured verdict after the requested marker.\n'
                    'Project context: TauCetiProject/TauCetiRoadmap and TauCeti/.\n'
                    'Content link: https://github.com/TauCetiProject/TauCeti/pull/7')
                (engine / 'rubrics/style.md').write_text('# Style\nReview the declaration style.')
                roadmap = root / 'roadmap-source'; roadmap.mkdir()
                (roadmap / 'Roadmap.md').write_text('Plan')
                binary = root / 'bin'; binary.mkdir()
                codex = binary / 'codex'
                calls = root / 'model-calls'
                last_prompt = root / 'last-prompt'
                (root / '.codex').mkdir()
                (root / '.codex/auth.json').write_text('{}')
                codex.write_text('#!' + sys.executable + '\n' + '''import json, os, re, sys
assert not any(key.startswith(('GH_', 'TAUCETI_FORGEJO_', 'TAUCETI_SOURCE_', 'GIT_CONFIG_')) for key in os.environ)
assert 'GIT_NO_LAZY_FETCH' not in os.environ
assert not any(key in os.environ for key in ('TAUCETI_CONF', 'TAUCETI_CONF_SOURCE', 'TAUCETI_CONF_SNAPSHOT'))
shared = ''
turns = []
def send(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
 message = json.loads(line)
 method, params = message.get('method'), message.get('params', {})
 if method == 'initialized': continue
 result = {}
 if method == 'thread/start': result = {'thread': {'id': 'test-thread'}, 'model': params['model']}
 elif method == 'thread/turns/list': result = {'data': [{'id': t} for t in turns], 'nextCursor': None}
 elif method == 'thread/revert': turns = ['primer']
 elif method == 'turn/start':
  prompt = params['input'][0]['text']
  marker = re.search(r'TAUCETI-VERDICT-[0-9a-f]+', prompt)
  if marker:
   turn = 'rubric'
   text = marker[0]+'\\n'+json.dumps({'verdict':'approve','summary':'Style is clear.'})
   with open(__CALLS__, 'a') as stream: stream.write('called\\n')
   with open(__PROMPT__, 'w') as stream: stream.write(shared + prompt)
  else:
   turn, shared, text = 'primer', prompt, 'READY'
  turns.append(turn)
  send({'id': message['id'], 'result': {'turn': {'id': turn}}})
  send({'method':'item/completed','params': {'turnId':turn,'item': {'type':'agentMessage','text':text}}})
  send({'method':'thread/tokenUsage/updated','params': {'turnId':turn,'tokenUsage':{
   'total':{'inputTokens':len(turns)},'last':{'inputTokens':1,'outputTokens':1}}}})
  send({'method':'turn/completed','params': {'turn':{'id':turn,'status':'completed'}}})
  continue
 send({'id': message['id'], 'result': result})
'''.replace('__CALLS__', repr(str(calls))).replace('__PROMPT__', repr(str(last_prompt))))
                codex.chmod(0o700)
                model_program = codex.read_text()
                configuration = root / 'project.toml'
                content = ('"Owner/Project"' if kind == 'github'
                           else '{forge="native",name="Owner/Project"}')
                configuration.write_text('schema_version = 2\n[forges.native]\nkind="forgejo"\n'
                    'url="https://git.example.org"\n[repositories]\ncontent=' + content + '\n'
                    'roadmap="Owner/Roadmap"\n[lean]\nroots=["Project"]\n')
                configuration_source = 'https://invalid.test/trusted-project.toml'
                stack.enter_context(patch.dict(os.environ, dict(
                    HOME=str(root), PATH=str(binary) + os.pathsep + os.environ.get('PATH', ''),
                    MODEL_CALLS=str(calls), GH_TOKEN='test-secret', GIT_NO_LAZY_FETCH='1',
                    TAUCETI_FORGEJO_TOKEN_FILE='/private/native-token',
                    TAUCETI_SOURCE_BINDING='private-test-binding', TAUCETI_REVIEW_REF='',
                    TAUCETI_CONF=configuration_source, TAUCETI_CONF_SOURCE=configuration_source,
                    TAUCETI_CONF_SNAPSHOT=str(configuration),
                    TAUCETI_REVIEW_MATHLIB_CACHE=str(root / 'shared-mathlib'))))
                stack.enter_context(patch.object(cli, 'review_source', lambda _: w.source))
                refs = stack.enter_context(patch.object(cli, 'pr_ref_oids', return_value=(HEAD, 'b' * 40)))
                stack.enter_context(patch.object(cli, 'merge_base_sha', return_value='b' * 40))
                stack.enter_context(patch.object(cli, 'gh_json', return_value={'title': 'Proof', 'body': 'Review this'}))
                stack.enter_context(patch.object(cli, 'rubrics_repo_sha', return_value=('c' * 40, False)))
                stack.enter_context(patch.object(cli, 'rubrics_publication', return_value=(False, True)))
                stack.enter_context(patch.object(cli, 'cli_sha', return_value=('d' * 40, False)))
                stack.enter_context(patch.object(cli, 'need'))
                def prepare(repo, head, merge_base, selected_work):
                    (selected_work / 'code').mkdir(exist_ok=True)
                    (selected_work / 'code/Proof.lean').write_text('theorem proof : True := trivial\n')
                    (selected_work / 'code/lake-manifest.json').write_text('{"packages":[]}')
                    (selected_work / 'diff.txt').write_text('diff --git a/Proof.lean b/Proof.lean\n+-- ' + head + '\n+theorem proof : True := trivial\n')
                    (selected_work / 'paths.z').write_bytes(b'Proof.lean\0')
                stack.enter_context(patch.object(cli, 'prepare_review_source', side_effect=prepare))
                def mathlib(manifest, destination, cache):
                    assert pathlib.Path(cache) == root / 'shared-mathlib'
                    destination.mkdir(exist_ok=True)
                    (destination / 'Mathlib.lean').write_text('-- source context')
                    refs.return_value = (w.api.head, 'f' * 40)  # main advanced during preparation
                    return True
                prepare_mathlib = stack.enter_context(patch.object(cli, 'prepare_mathlib_source', side_effect=mathlib))
                real_run = cli.run
                clones = []
                def run(command, **kwargs):
                    if command[:2] == ['git', 'clone']:
                        clones.append(command[-2])
                        destination = pathlib.Path(command[-1]); destination.mkdir(exist_ok=True)
                        (destination / 'Roadmap.md').write_text('Plan')
                        return type('Result', (), dict(returncode=0))()
                    return real_run(command, **kwargs)
                stack.enter_context(patch.object(cli, 'run', side_effect=run))
                result_file = root / 'result.json' 
                stack.enter_context(patch.dict(os.environ, {'TAUCETI_REVIEW_RESULT_FILE': str(result_file)}))
                argv = ['tauceti-review', '7', '--repo', w.source.key, '--expect-head', HEAD,
                        '--repo-dir', str(engine), '--workdir', str(work),
                        '--store', str(w.ledger.parent), '--post', '--no-sync', '--no-coordinate',
                        '--reviewer', 'codex', '--rubrics', 'style']
                stack.enter_context(patch.object(sys, 'argv', argv))
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    assert cli.main() in (None, 0)
                prepare_mathlib.assert_called_once()
                assert clones == ['https://github.com/TauCetiProject/TauCetiRoadmap']
                assert json.loads(result_file.read_text()) == dict(head_sha=HEAD, changed=True)
                assert calls.read_text() == 'called\n'
                assert 'Owner/Roadmap and Project/' in last_prompt.read_text()
                assert 'TauCetiProject/TauCetiRoadmap' not in last_prompt.read_text()
                host = 'github.com' if kind == 'github' else 'git.example.org'
                route = 'pull' if kind == 'github' else 'pulls'
                assert f'https://{host}/Owner/Project/{route}/7' in last_prompt.read_text()
                if kind == 'forgejo':
                    assert 'https://github.com/Owner/Project' not in last_prompt.read_text()
                receipt = json.loads(w.receipt.read_text())
                assert receipt['state'] == 'published'
                assert w.source.repository.url + '/compare/' in receipt['scoreboard']
                assert 'https://github.com/forgejo:' not in receipt['scoreboard']
                state = json.loads(w.ledger.read_text())['prs']['7']
                assert state['published_head_sha'] == HEAD
                records = list((w.ledger.parent / 'outbox/records/runs').rglob('*.json'))
                assert records and json.loads(records[0].read_text())['repo'] == w.source.key
                assert not (work / 'code').exists() and not (work / 'mathlib').exists()
                # The same CLI/engine must prove this is already published, without inference.
                codex.write_text('#!' + sys.executable + "\nraise RuntimeError('second model call')\n")
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    assert cli.main() in (None, 0)
                assert json.loads(result_file.read_text()) == dict(head_sha=HEAD, changed=False)
                assert calls.read_text() == 'called\n'
                assert not (work / 'code').exists()
                # A changed head enters the same engine again and publishes a new
                # exact-head result rather than reusing the unchanged receipt.
                next_head = 'e' * 40
                w.api.head = next_head
                refs.return_value = (next_head, 'b' * 40)
                argv[argv.index('--expect-head') + 1] = next_head
                codex.write_text(model_program)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    assert cli.main() in (None, 0)
                assert calls.read_text() == 'called\ncalled\n'
                assert json.loads(result_file.read_text()) == dict(head_sha=next_head, changed=True)
                assert json.loads(w.ledger.read_text())['prs']['7']['published_head_sha'] == next_head

                # An author can contest a previously passing finding. Both
                # discussion shapes must invoke the same reply-only review.
                root_id, through = w.source.contest('Please reconsider this style finding.')
                ledger = json.loads(w.ledger.read_text())
                ledger['prs']['7']['state']['style']['thread'] = dict(comment_id=root_id, path='Proof.lean')
                w.ledger.write_text(json.dumps(ledger))
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    assert cli.main() in (None, 0)
                assert calls.read_text() == 'called\n' * 3
                assert 'Please reconsider this style finding.' in last_prompt.read_text()
                ledger = json.loads(w.ledger.read_text())
                assert ledger['prs']['7']['rounds'][-1]['mode'] == 'reply'
                assert ledger['prs']['7']['rounds'][-1]['ran'] == ['style']
                answered = [row for row in w.source.findings(7)
                            if f'<!--tauceti-reply:style:through:{through}-->' in row['body']]
                assert len(answered) == 1 and answered[0]['in_reply_to_id'] == root_id
                codex.write_text('#!' + sys.executable + "\nraise RuntimeError('adjudicated contest reran')\n")
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    assert cli.main() in (None, 0)
                assert calls.read_text() == 'called\n' * 3
                assert json.loads(result_file.read_text()) == dict(head_sha=next_head, changed=False)

                # A new head with unavailable preparation leaves paid output,
                # archives and inference count intact, then the normal retry runs.
                after_contest = 'f' * 40
                w.api.head = after_contest
                refs.return_value = (after_contest, 'b' * 40)
                argv[argv.index('--expect-head') + 1] = after_contest
                before_records = {path: path.read_bytes() for path in
                    (w.ledger.parent / 'outbox').rglob('*.json')}
                with patch.object(cli, 'prepare_review_source',
                                  side_effect=cli.repository_diff.PreparationUnavailable('HTTP 503')):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        assert cli.main() == cli.PREPARATION_WAIT_EXIT
                assert calls.read_text() == 'called\n' * 3
                assert not (work / 'code').exists()
                assert all(path.read_bytes() == value for path, value in before_records.items())
                codex.write_text(model_program)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    assert cli.main() in (None, 0)
                assert calls.read_text() == 'called\n' * 4
                assert json.loads(result_file.read_text()) == dict(head_sha=after_contest, changed=True)



if __name__ == '__main__':
    unittest.main()
