"""A completed PR review survives handoff to an empty worker store."""
import contextlib
import copy
import json
import pathlib
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / 'runner')]
import cli
import casefile
import test_billing as tb


def scenario(states=None, *, board_changes=None, replies=None, local=None, mode='commit',
             unavailable=False, verdicts=None):
    rubrics = ['reuse', 'scope']
    d, rd, store = tb._workspace(rubrics)
    board = {'comment_id': 7, 'by': 'other-worker', 'head_sha': tb.HEAD,
             'states': states or {'reuse': 'green', 'scope': 'green'}, 'replies_through': 10}
    board.update(board_changes or {})
    prior, reply_file, plan, sb = [d / n for n in ('prior.json', 'replies.json', 'plan.json', 'scoreboard.md')]
    prior.write_text(json.dumps(board))
    if not unavailable:
        reply_file.write_text(json.dumps(replies or {}))
    ledger = store / 'ledger.json'
    ledger.write_text(json.dumps({'days': {}, 'prs': {'1': local} if local else {}}))
    before = ledger.read_bytes()
    tb._install_stubs()
    tb._VERDICTS.clear(); tb._VERDICTS.update(verdicts or {})
    calls = []

    def run(prompt, cwd, model, env):
        calls.append(prompt)
        return tb._fake_runner(prompt, cwd, model, env)

    with patch.object(tb.review, 'run_claude', side_effect=run):
        after = tb._run(store, rd, d / 'diff.txt', mode=mode, rubrics=rubrics, extra=[
            '--prior-scoreboard-json', str(prior), '--replies-json', str(reply_file),
            '--post-plan-file', str(plan), '--scoreboard-file', str(sb)])
    result = json.loads(plan.read_text())
    if result['review_changed'] is False:
        assert not calls and ledger.read_bytes() == before
        assert not (store / 'reviews').exists(), 'no new archive/round for unchanged published work'
    return after, result, calls, sb.read_text()


def test_handoff():
    # Different identities and two workers of the same identity are equivalent.
    for by in ('other-reviewer', 'tauceti-ai-for-science'):
        after, plan, calls, _ = scenario(board_changes={'by': by})
        assert plan['review_changed'] is False and after == {'days': {}, 'prs': {}}
    for states in (
        {'reuse': 'blocking_request', 'scope': 'stale'},
        {'reuse': 'blocking_block', 'scope': 'absent'},
    ):
        assert scenario(states)[1]['review_changed'] is False
    # A known adjudicated contest does not start a new round in a fresh store.
    reply = {'id': 10, 'root_id': 42, 'body': 'Please reconsider', 'by': 'author',
             'root_body': 'Original finding: the unnecessary hypothesis can be removed.'}
    assert scenario(replies={'reuse': [reply]})[1]['review_changed'] is False
    # A NEW contest runs only its rubric, retains the other decisions, and replies to its root.
    reply['id'] = 11
    for states in (
        {'reuse': 'green', 'scope': 'green'},
        {'reuse': 'blocking_request', 'scope': 'green'},
        {'reuse': 'blocking_request', 'scope': 'blocking_request'},
    ):
        after, plan, calls, sb = scenario(states, replies={'reuse': [reply]})
        pr = after['prs']['1']; last = pr['rounds'][-1]
        assert len(calls) == 1 and last['ran'] == ['reuse'] and last['mode'] == 'reply'
        assert 'Please reconsider' in calls[0] and 'Original finding' in calls[0]
        assert last['states']['scope'] == states['scope']
        assert 'scope' not in pr['state'], 'retained decisions must not invent private model case files'
        assert all(t['rubric'] == 'reuse' for t in plan['threads'])
        assert any(t['action'] == 'reply' and t['in_reply_to'] == 42 for t in plan['threads'])
        meta = json.loads(re.findall(cli.META_RE, sb)[-1])
        assert meta['states'] == last['states'] and meta['replies_through'] == 11
        assert meta['reused_scoreboard_comment_id'] == 7
    # Changed/incomplete/error/init states retain normal review rather than being skipped.
    for states, changes in (
        ({'reuse': 'green'}, {}),
        ({'reuse': 'error', 'scope': 'green'}, {}),
        ({'reuse': 'stale', 'scope': 'green'}, {}),
        (None, {'head_sha': 'b' * 40}),
        (None, {'mode': 'init'}),
        (None, {'rubrics_version': 'different rubric text'}),
    ):
        assert scenario(states, board_changes=changes)[2]
    assert scenario(mode='manual')[2], 'forced review still runs'
    assert scenario(unavailable=True)[2], 'unavailable replies must not establish no contest or block work'
    # A pending publication and an init replacement still repair without inference.
    local = {'rounds': [], 'state': {r: {'verdict': 'approve', 'approved_sha': tb.HEAD}
                                    for r in ('reuse', 'scope')},
             'scoreboard_comment_id': 500, 'published_head_sha': tb.HEAD,
             'pending_publication_head_sha': tb.HEAD}
    after, plan, calls, _ = scenario(local=local)
    assert plan['review_changed'] is True and not calls
    init_local = copy.deepcopy(local)
    init_local.pop('pending_publication_head_sha'); init_local.pop('published_head_sha')
    assert scenario(local=init_local)[1]['review_changed'] is True
    # Existing local findings cannot be erased by an imported green scoreboard.
    local['state']['reuse'] = {'rubric': 'reuse', 'verdict': 'request_changes', 'reviewed_sha': tb.HEAD,
                              'run_id': 'existing', 'summary': 'Keep this finding', 'thread': None}
    after, plan, _, _ = scenario(local=local)
    assert after['prs']['1']['state']['reuse']['summary'] == 'Keep this finding'
    assert any(t['required'] and t['rubric'] == 'reuse' for t in plan['threads'])
    # The same existing GET includes the original finding and completion watermark.
    root = {'id': 42, 'body': '<!--tauceti-rubric:reuse--> Original finding'}
    response = SimpleNamespace(returncode=0, stdout='\n'.join(map(json.dumps, [root, {
        'id': 11, 'in_reply_to_id': 42, 'body': 'Please reconsider', 'user': {'login': 'author'}}])))
    with patch.object(cli, 'run', return_value=response) as get:
        fetched = cli.fetch_thread_replies('Owner/Repo', 1)
    assert get.call_count == 1 and fetched['reuse'][0]['root_body'] == root['body']
    with patch.object(cli, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
        assert cli.fetch_thread_replies('Owner/Repo', 1) is None
    c = {'id': 7, 'user': {'login': 'reviewer'}, 'body': '<!--tauceti-scoreboard-->\n'
         '<!--tauceti-meta:v1 ' + json.dumps({'head_sha': tb.HEAD, 'states': {'reuse': 'green'},
                                           'replies_through': 10, 'rubrics_version': 'V'}) + '-->'}
    board = cli.latest_scoreboard([c])
    assert board['replies_through'] == 10 and board['rubrics_version'] == 'V'
    print('PASS: completed handoffs spend/write nothing; contests retain unaffected decisions and original findings; repairs and blind work continue')


if __name__ == '__main__':
    test_handoff()
