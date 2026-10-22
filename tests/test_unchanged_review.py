"""Completed heads do not republish or create fictitious review rounds."""
import json
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / 'runner')]
import cli
import post
import test_billing as tb


def test_unchanged_review():
    tb._install_stubs()
    tb._VERDICTS.clear()
    d, rd, store = tb._workspace(['reuse'])
    plan = d / 'plan.json'
    args = ['--post-plan-file', str(plan)]
    before = tb._run(store, rd, d / 'diff.txt', mode='commit', rubrics=['reuse'], extra=args)
    before['prs']['1']['published_head_sha'] = tb.HEAD
    before['prs']['1'].pop('pending_publication_head_sha', None)
    ledger = store / 'ledger.json'
    ledger.write_text(json.dumps(before))
    # Repeated engine invocations leave the ledger and archive round counts unchanged.
    for _ in range(3):
        after = tb._run(store, rd, d / 'diff.txt', mode='commit', rubrics=['reuse'], extra=args)
        assert after == before
        assert json.loads(plan.read_text())['review_changed'] is False
    with patch.object(post, 'gh_api', side_effect=AssertionError('unexpected GitHub write')), \
         patch.object(post, 'current_login', side_effect=AssertionError('unexpected auth lookup')):
        assert post.execute_post('Owner/Repo', 1, json.loads(plan.read_text()), '/no-ledger-needed') == 0
    # A pending publication must still be repaired even though no new model runs.
    before['prs']['1']['pending_publication_head_sha'] = tb.HEAD
    ledger.write_text(json.dumps(before))
    tb._run(store, rd, d / 'diff.txt', mode='commit', rubrics=['reuse'], extra=args)
    assert json.loads(plan.read_text())['review_changed'] is True
    before['prs']['1'].pop('pending_publication_head_sha')
    ledger.write_text(json.dumps(before))
    # An unchanged patch carried to a new head still needs a current-head publication.
    old_head = tb.HEAD
    try:
        tb.HEAD = 'b' * 40
        moved = tb._run(store, rd, d / 'diff.txt', mode='commit', rubrics=['reuse'], extra=args)
        assert json.loads(plan.read_text())['review_changed'] is True
        assert moved['prs']['1']['rounds'][-1]['mode'] == 'carry'
        assert moved['prs']['1']['rounds'][-1]['ran'] == []
    finally:
        tb.HEAD = old_head
    # A new contest on the completed head remains real review work.
    ledger.write_text(json.dumps(before))
    replies = d / 'replies.json'
    replies.write_text(json.dumps({'reuse': [{'id': 10, 'root_id': 5, 'by': 'author', 'body': 'Please reconsider.'}]}))
    contested = tb._run(store, rd, d / 'diff.txt', mode='commit', rubrics=['reuse'],
                       extra=args + ['--replies-json', str(replies)])
    assert json.loads(plan.read_text())['review_changed'] is True
    assert contested['prs']['1']['rounds'][-1]['ran'] == ['reuse']
    with tempfile.TemporaryDirectory() as tmp:
        result = pathlib.Path(tmp) / 'result.json'
        with patch.dict(os.environ, {'TAUCETI_REVIEW_RESULT_FILE': str(result)}):
            cli.write_review_result(old_head, False)
        assert json.loads(result.read_text()) == {'head_sha': old_head, 'changed': False}
    print('PASS: repeated completed reviews have no writes or new rounds; repair, carry and contests still progress')


if __name__ == '__main__':
    test_unchanged_review()
