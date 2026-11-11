"""The same publication/recovery scenarios run against both native transports."""
import json
import pathlib
import sys
import types

import contextlib
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from runner import post, publication, publication_transport
from tauceti_tools.ci_candidate import RepositoryIdentity
from tauceti_tools.forge import AmbiguousWrite, Forge, RepositoryRef
from tauceti_tools.review_source import review_comments


HEAD = 'a' * 40


class API:
    def __init__(self):
        self.actor = dict(id=3, login='author')
        self.head = HEAD
        self.comments = []
        self.findings = []
        self.groups = []
        self.writes = []
        self.lose_response = None
        self.move_after_root = False
        self.sequence = 10

    def _row(self, body, **other):
        self.sequence += 1
        return dict(id=self.sequence, body=body, user=dict(self.actor),
                    created_at='2026-11-11T00:00:00Z', updated_at=f'2026-11-11T00:00:{self.sequence:02}Z', **other)

    def request(self, method, endpoint, data=None):
        endpoint = endpoint.lstrip('/')
        self.writes.append((method, endpoint))
        if method == 'POST' and endpoint.endswith('/reviews'):
            result = dict(id=self.sequence + 1, commit_id=data['commit_id'])
            self.sequence += 1
            rows = [self._row(row['body'], commit_id=data['commit_id'], path=row['path'],
                              position=row['new_position'], original_position=row['old_position'])
                    for row in data['comments']]
            self.groups.append(dict(**result, comments=rows))
        elif method == 'POST' and '/pulls/' in endpoint and endpoint.endswith('/comments'):
            result = self._row(data['body'], commit_id=data['commit_id'], path=data['path'])
            self.findings.append(result)
        elif method == 'POST' and endpoint.endswith('/comments'):
            result = self._row(data['body'])
            self.comments.append(result)
        elif method == 'PATCH':
            identifier = int(endpoint.rsplit('/', 1)[-1])
            rows = [*self.comments, *self.findings, *(row for group in self.groups for row in group['comments'])]
            result = next(row for row in rows if row['id'] == identifier)
            result['body'] = data['body']
        elif method == 'DELETE':
            identifier = int(endpoint.rsplit('/', 1)[-1])
            self.comments[:] = [row for row in self.comments if row['id'] != identifier]
            result = {}
        else:
            raise AssertionError((method, endpoint, data))
        if method == 'POST' and '/pulls/' in endpoint and self.move_after_root:
            self.head = 'b' * 40
        if self.lose_response and self.lose_response in endpoint:
            self.lose_response = None
            raise AmbiguousWrite('accepted write response lost')
        return result

    def reviews(self, repository, number):
        return self.groups

    def review_comments(self, repository, number, identifier):
        return next(group['comments'] for group in self.groups if group['id'] == identifier)


class Source:
    def __init__(self, api, repository):
        self.client, self.repository, self.key = api, repository, repository.name

    def pull(self, number):
        return dict(state='open', head=dict(sha=self.client.head))

    def publisher_key(self):
        return self.client.actor['login']

    def comments(self, number):
        return self.client.comments

    def findings(self, number):
        return self.client.findings


class NativeSource(Source):
    def __init__(self, api, repository):
        super().__init__(api, repository)
        self.api, self.key, self.identity = api, repository.key, RepositoryIdentity(repository, 1)

    def publisher_key(self):
        return self.repository.forge.key + '/users/' + str(self.client.actor['id'])

    def findings(self, number):
        # The real normalizer supplies containing-review heads and native replies.
        return review_comments(self.api, self.repository, number, conversation=self.comments(number))


def workflow(kind, tmp_path, stack):
    store = tmp_path
    tmp_path = tmp_path / "review-work" / "one"
    tmp_path.mkdir(parents=True)
    api = API()
    forge = Forge(kind, 'https://github.com' if kind == 'github' else 'https://git.example.org')
    source = (Source if kind == 'github' else NativeSource)(api, RepositoryRef(forge, 'team/project'))
    stack.enter_context(patch.object(publication, 'source_for', lambda repo: source))
    stack.enter_context(patch.object(publication_transport, 'source_for', lambda repo: source))
    stack.enter_context(patch.object(post, 'current_login', source.publisher_key))
    stack.enter_context(patch.object(post, 'find_review_roots', lambda repo, pr: post.normalize_roots(source.findings(pr))))
    stack.enter_context(patch.object(post, 'find_scoreboard_comments', lambda repo, pr: [
        dict(row, login=row['user']['login']) for row in reversed(api.comments)
        if '<!--tauceti-scoreboard-->' in row['body']]))
    def gh_api(method, endpoint, fields=None, body_file=None, failures=None, action=''):
        data = dict(fields or {})
        if body_file:
            data['body'] = pathlib.Path(body_file).read_text()
        try:
            return api.request(method, endpoint, data)
        except AmbiguousWrite:
            if failures is not None:
                failures.append(dict(action=action, error='response lost'))
            return None
    stack.enter_context(patch.object(post, 'gh_api', gh_api))
    ledger = store / 'ledger.json'
    ledger.write_text(json.dumps(dict(days={}, prs={'7': dict(state={}, pending_publication_head_sha=HEAD)})))
    meta = json.dumps(dict(head_sha=HEAD, runs=[dict(id='run-1')]))
    body = tmp_path / 'finding.md'
    body.write_text('<!--tauceti-rubric:correctness-->\nFinding\n<!--tauceti-meta:v1 ' + meta + '-->')
    board = tmp_path / 'scoreboard.md'; board.write_text('<!--tauceti-scoreboard-->\nBlocked')
    (tmp_path / 'diff.txt').write_text('diff --git a/TauCeti/A.lean b/TauCeti/A.lean\n--- a/TauCeti/A.lean\n+++ b/TauCeti/A.lean\n@@ -1 +1 @@\n-old\n+new\n')
    plan = tmp_path / 'post_plan.json'
    plan.write_text(json.dumps(dict(head_sha=HEAD, round=1, scoreboard_body=str(board), threads=[
        dict(action='upsert', required=True, rubric='correctness', run_id='run-1',
             path='TauCeti/A.lean', body=str(body))])))
    receipt = publication.retain(source.key, 7, plan, ledger)
    return types.SimpleNamespace(api=api, source=source, ledger=ledger, receipt=receipt)


def publish(w):
    return publication.publish(w.source.key, w.receipt, w.ledger)


class SharedPublication(unittest.TestCase):

    def test_required_root_then_board_and_idempotent_replay(self):
        for kind in ("github", "forgejo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                tmp_path = pathlib.Path(directory)
                w = workflow(kind, tmp_path, stack)
                assert publish(w) == 0
                assert len(w.source.findings(7)) == 1 and len(w.api.comments) == 1
                state = json.loads(w.ledger.read_text())['prs']['7']
                assert state['published_head_sha'] == HEAD and 'pending_publication_head_sha' not in state
                assert state['state']['correctness']['thread']['comment_id'] == w.source.findings(7)[0]['id']
                writes = list(w.api.writes)
                assert publish(w) == 0 and w.api.writes == writes

    def test_lost_accepted_write_recovers_without_new_finding(self):
        for kind in ("github", "forgejo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                tmp_path = pathlib.Path(directory)
                w = workflow(kind, tmp_path, stack)
                w.api.lose_response = '/pulls/'
                assert publish(w) == 85
                assert len(w.source.findings(7)) == 1 and not w.api.comments
                assert publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)
                assert len(w.source.findings(7)) == 1 and len(w.api.comments) == 1

    def test_stale_before_or_during_publication_withholds_board(self):
        for kind in ("github", "forgejo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                tmp_path = pathlib.Path(directory)
                w = workflow(kind, tmp_path, stack)
                w.api.move_after_root = True
                assert publish(w) == 82
                assert not w.api.comments
                assert json.loads(w.receipt.read_text())['state'] == 'superseded'

    def test_actor_and_store_binding_survive_retention(self):
        for kind in ("github", "forgejo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                tmp_path = pathlib.Path(directory)
                w = workflow(kind, tmp_path, stack)
                w.api.actor = dict(id=4, login='other')
                with self.assertRaisesRegex(ValueError, 'original publisher'):
                    publish(w)
                assert not w.api.writes
                with self.assertRaisesRegex(ValueError, 'source/store'):
                    publication.publish(w.source.key, w.receipt, tmp_path / 'foreign.json')

    def test_recovery_skips_a_live_producer_or_host_lease(self):
        for kind in ('github', 'forgejo'):
            for lease in ('live.lock', 'runner.lock'):
                with self.subTest(kind=kind, lease=lease), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                    w = workflow(kind, pathlib.Path(directory), stack)
                    with publication.workspace_lock(w.receipt.parent, lease):
                        assert not publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)
                    assert not w.api.writes
                    assert publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)

    def test_crash_before_retention_keeps_original_publisher(self):
        for kind in ('github', 'forgejo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                w = workflow(kind, pathlib.Path(directory), stack)
                w.receipt.unlink()
                (w.receipt.parent / 'review-context.json').write_text(json.dumps(dict(
                    source=publication.source_record(w.source), pr=7, head=HEAD, store=str(w.ledger),
                    publisher=w.source.publisher_key(), lifecycle=2)))
                w.api.actor = dict(id=4, login='other')
                with self.assertRaisesRegex(ValueError, 'original publisher'):
                    publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)
                assert not w.api.writes
                assert json.loads(w.receipt.read_text())['actor'].endswith('3' if kind == 'forgejo' else 'author')

    def test_legacy_native_paid_receipt_recovers_without_new_inference(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            w = workflow('forgejo', pathlib.Path(directory), stack)
            row = json.loads(w.receipt.read_text())
            for key in ('version', 'actor', 'repo'):
                row.pop(key)
            row['actor_id'] = 3
            row['threads'][0].update(new_position=1, old_position=0)
            w.receipt.write_text(json.dumps(row))
            (w.receipt.parent / 'diff.txt').unlink()
            assert publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)
            assert len(w.source.findings(7)) == 1
            assert json.loads(w.ledger.read_text())['prs']['7']['published_head_sha'] == HEAD

    def test_native_conversation_edit_preserves_head_and_reply_identity(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            w = workflow('forgejo', pathlib.Path(directory), stack)
            (w.receipt.parent / 'diff.txt').unlink()
            assert publish(w) == 0
            root = w.source.findings(7)[0]
            row = json.loads(w.receipt.read_text())
            row['state'] = 'prepared'
            row['threads'][0]['run_id'] = 'run-2'
            row['threads'][0]['body'] = row['threads'][0]['body'].replace('run-1', 'run-2')
            w.receipt.write_text(json.dumps(row))
            assert publish(w) == 0
            roots = w.source.findings(7)
            assert len(roots) == 1 and roots[0]['id'] == root['id']
            assert roots[0]['commit_id'] == HEAD and roots[0]['thread_kind'] == 'issue-comment'

    def test_failed_recovery_keeps_output_and_requests_publication_retry(self):
        for kind in ('github', 'forgejo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                w = workflow(kind, pathlib.Path(directory), stack)
                w.api.lose_response = '/pulls/'
                with self.assertRaises(publication.PublicationPending):
                    publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)
                assert json.loads(w.receipt.read_text())['state'] == 'prepared'
                assert publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)
                assert len(w.source.findings(7)) == 1

    def test_disabled_archive_survives_retention_and_recovery(self):
        for kind in ('github', 'forgejo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                w = workflow(kind, pathlib.Path(directory), stack)
                value = json.loads(w.receipt.read_text())
                value['archive_dir'] = ''
                w.receipt.write_text(json.dumps(value))
                assert publication.recover(w.source.key, w.receipt.parent.parent, w.ledger)
                assert not (w.ledger.parent / 'outbox').exists()

    def test_native_review_commit_is_authoritative_over_edited_body_metadata(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            w = workflow('forgejo', pathlib.Path(directory), stack)
            assert publish(w) == 0
            next_head = 'b' * 40
            row = w.api.groups[0]['comments'][0]
            row['body'] = row['body'].replace(HEAD, next_head)
            transport = publication_transport.NativePublication(w.source.key, 7, next_head,
                {}, post, source=w.source)
            root = transport.roots()[0]
            assert root['head_sha'] == HEAD
            assert not transport.editable(root)


if __name__ == "__main__":
    unittest.main()
