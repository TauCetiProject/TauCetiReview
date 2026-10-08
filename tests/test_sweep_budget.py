import contextlib
import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'runner'))
import api_budget
import backend
import sweep


class SweepBudgetTests(unittest.TestCase):
    def tearDown(self):
        api_budget.limit = None
        api_budget.calls = 0
        api_budget.rate_limited = False
        api_budget.failures = 0

    def setUp(self):
        self.publisher = patch.object(backend, 'publish_eligibility').start()
        self.addCleanup(patch.stopall)

    def test_quota_headroom_and_allowance_to_finish_a_pr(self):
        quota = {'resources': {'core': {'remaining': 1050}, 'graphql': {'remaining': 5000}}}
        with patch.object(api_budget.subprocess, 'run', return_value=subprocess.CompletedProcess('gh', 0, json.dumps(quota), '')), \
                contextlib.redirect_stdout(io.StringIO()):
            api_budget.configure(True)
        self.assertEqual(api_budget.limit, 50)
        api_budget.calls = 21
        with self.assertRaises(api_budget.Exhausted):
            api_budget.begin_pr()
        # The PR already in flight can finish its immediate integrity/revocation work.
        with patch.object(api_budget.subprocess, 'run', return_value=subprocess.CompletedProcess('gh', 0, '{}', '')):
            self.assertEqual(api_budget.run(['gh', 'api', 'endpoint']).returncode, 0)

    def test_rate_limit_aborts_without_trying_every_remaining_pr(self):
        result = subprocess.CompletedProcess('gh', 1, '', 'gh: API rate limit exceeded for installation ID 1')
        with patch.object(api_budget.subprocess, 'run', return_value=result) as gh:
            with self.assertRaises(api_budget.Exhausted):
                sweep.sweep_bors([{'number': 1}, {'number': 2}, {'number': 3}])
            self.assertEqual(gh.call_count, 1)

    def test_focused_scan_prioritizes_both_queues_and_preserves_unsafe_withdrawals(self):
        prs = [{'number': n, 'isDraft': n == 1, 'labels': []} for n in range(1, 181)]
        prs[1]['labels'] = [{'name': 'keep'}]  # held bors approval still needs withdrawal
        prs[2]['labels'] = [{'name': 'ready-to-merge'}]
        withdrawn = []
        h, mb = 'a' * 40, 'b' * 40
        for mode in ('queue', 'bors', 'unknown'):
            withdrawn.clear()
            def read(args):
                if args[:2] == ['pr', 'view']:
                    n = int(args[2])
                    return {'headRefOid': h, 'baseRefName': 'main', 'baseRefOid': mb,
                            'id': str(n), 'labels': [], 'statusCheckRollup': []}
                if '/compare/' in args[1]:
                    return {'merge_base_commit': {'sha': mb}}
                raise AssertionError(args)
            with patch.object(sweep, 'REPO', 'o/r'), patch.object(sweep, 'FOCUSED', True), \
                    patch.object(backend, 'selected', return_value={'backend': mode}), \
                    patch.object(backend, 'github_entries', return_value=[{'number': 1}]), \
                    patch.object(sweep, 'bors_approved_prs', return_value={2}), \
                    patch.object(sweep, 'queue_entries', return_value=[{'number': 1}]), \
                    patch.object(sweep, 'open_prs', return_value=prs), \
                    patch.object(backend, 'allow', return_value=True), \
                    patch.object(sweep, 'gh_json', side_effect=read), \
                    patch.object(sweep, 'gh_jsonl', return_value=[]), \
                    patch.object(sweep, 'merge_base_now', return_value=mb), \
                    patch.object(sweep, 'current_head', return_value=h), \
                    patch.object(sweep, 'pr_diff', return_value=['TauCeti/X.lean']), \
                    patch.object(sweep, 'decide_from_comments', return_value={'review_safe': False, 'merge': False}), \
                    patch.object(sweep, 'withdraw_both', side_effect=lambda n, *args: withdrawn.append(n) or True), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sweep.main(), 0)
            self.assertEqual(set(withdrawn), {1, 2, 3}, mode)
            if mode == 'unknown':
                self.assertEqual(set(withdrawn[:2]), {1, 2}, mode)
            else:
                self.assertIn(withdrawn[0], {1, 2}, mode)
                self.assertEqual(withdrawn[1], 3, mode)

    def test_a_long_queue_does_not_starve_labelled_prs_awaiting_admission(self):
        prs = [{'number': n, 'isDraft': False, 'labels': []} for n in range(1, 71)]
        for p in prs[-2:]:
            p['labels'] = [{'name': 'ready-to-merge'}]
        with patch.object(sweep, 'FOCUSED', True):
            order = [p['number'] for p in sweep.candidates(prs, set(range(1, 61)), {61, 62})]
        self.assertEqual(len(order), 64)
        self.assertLessEqual(order[0], 62)
        self.assertEqual(set(order[1:4:2]), {69, 70})

    def test_large_queue_reaches_admission_and_withdrawal_within_focused_budget(self):
        # Exercise the actual queue-path reader and candidate loop with more
        # approvals than one sweep can inspect. Queued PRs are unsafe; waiting
        # PRs are green. Both withdrawal and admission must make progress.
        h, mb = 'a' * 40, 'b' * 40
        prs = [{'number': n, 'isDraft': False, 'labels': [{'name': 'ready-to-merge'}]}
               for n in range(1, 241)]
        entries = [{'number': n, 'node_id': str(n), 'head_sha': h,
                    'enqueued_at': '2026-10-08T00:00:00Z', 'paths': ['TauCeti/X.lean']}
                   for n in range(1, 181)]
        withdrawn, admitted = [], []
        api_budget.limit = 120
        api_budget.calls = 8  # quota, backend, open PRs and paginated queue reads
        visited = []

        def charge(count=1):
            api_budget.calls += count

        def read(args):
            charge()
            if args[:2] == ['pr', 'view']:
                n = int(args[2])
                visited.append(n)
                return {'headRefOid': h, 'baseRefName': 'main', 'baseRefOid': mb,
                        'id': str(n), 'labels': [], 'statusCheckRollup': [],
                        'isCrossRepository': False}
            if '/compare/' in args[1]:
                return {'merge_base_commit': {'sha': mb}, 'behind_by': 0}
            if '/commits/' in args[1]:
                return {'commit': {'committer': {'date': '2026-10-08T00:00:00Z'}}}
            raise AssertionError(args)

        def decision(comments, head, *args, **kwargs):
            queued = comments[0]['pr'] <= 180
            return {'review_safe': not queued, 'merge': not queued}

        def comments(args):
            charge()
            endpoint = next(a for a in args if '/issues/' in a)
            if '/comments?' in endpoint:
                return [{'pr': int(endpoint.split('/issues/')[1].split('/')[0])}]
            return []

        def withdraw(n, *args):
            charge(5)
            withdrawn.append(n)
            return True

        def enqueue(n, *args):
            charge(3)
            admitted.append(n)
            return True

        def publish(*args, **kwargs):
            charge(3)

        def current_head(*args):
            charge()
            return h

        def merge_base(*args):
            charge(2)
            return mb

        with patch.object(sweep, 'REPO', 'o/r'), patch.object(sweep, 'FOCUSED', True), \
                patch.object(sweep, 'DRY_RUN', False), \
                patch.object(backend, 'selected', return_value={'backend': 'queue'}), \
                patch.object(backend, 'github_entries', return_value=entries), \
                patch.object(sweep, 'pr_paths', side_effect=AssertionError('per-entry REST read')), \
                patch.object(sweep, 'bors_approved_prs', return_value=set()), \
                patch.object(sweep, 'open_prs', return_value=prs), \
                patch.object(backend, 'allow', return_value=True), \
                patch.object(sweep, 'gh_json', side_effect=read), \
                patch.object(sweep, 'gh_jsonl', side_effect=comments), \
                patch.object(sweep, 'merge_base_now', side_effect=merge_base), \
                patch.object(sweep, 'current_head', side_effect=current_head), \
                patch.object(sweep, 'pr_diff', return_value=['TauCeti/X.lean']), \
                patch.object(sweep, 'decide_from_comments', side_effect=decision), \
                patch.object(sweep, 'withdraw_both', side_effect=withdraw), \
                patch.object(sweep, 'enqueue', side_effect=enqueue), \
                patch.object(backend, 'publish_eligibility', side_effect=publish), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(api_budget.Exhausted):
                sweep.main()
        self.assertGreaterEqual(len(withdrawn), 3)
        self.assertGreaterEqual(len(admitted), 3)
        self.assertEqual(visited[1], admitted[0])
        self.assertTrue(all(n > 180 for n in admitted))
        self.assertLess(api_budget.calls, api_budget.limit)

    def test_paused_admission_prioritizes_withdrawals_without_dropping_waiting_prs(self):
        prs = [{'number': n, 'isDraft': False,
                'labels': [{'name': 'ready-to-merge'}]} for n in range(1, 71)]
        with patch.object(sweep, 'FOCUSED', True):
            order = sweep.candidates(prs, set(range(1, 61)), {61, 62}, admit=False)
        self.assertEqual({p['number'] for p in order[:62]}, set(range(1, 63)))
        self.assertEqual({p['number'] for p in order[62:]}, set(range(63, 71)))

    def test_budget_deferral_is_not_swallowed_by_admission_or_revocation(self):
        with patch.object(backend, 'selected', side_effect=api_budget.Exhausted('limited')):
            with self.assertRaises(api_budget.Exhausted):
                backend.allow('o/r', 'bors')
        with patch.object(sweep, 'dequeue', return_value=True), \
                patch.object(backend, 'publish_eligibility', side_effect=api_budget.Exhausted('limited')):
            with self.assertRaises(api_budget.Exhausted):
                sweep.withdraw_both(1, 'PR1', 'a' * 40)

    def test_hourly_scan_retains_background_work_and_focused_scan_excludes_it(self):
        prs = [{'number': n, 'isDraft': False, 'labels': []} for n in range(1, 181)]
        for focused, expected in ((True, {1}), (False, set(range(1, 181)))):
            with patch.object(sweep, 'FOCUSED', focused):
                actual = sweep.candidates(prs, {1}, set())
                self.assertEqual({p['number'] for p in actual}, expected)
                self.assertEqual(actual[0]['number'], 1)

    def test_unreadable_quota_is_an_error_not_a_successful_deferral(self):
        for response in (subprocess.CompletedProcess('gh', 1, '', 'permission denied'),
                         subprocess.CompletedProcess('gh', 0, '{}', '')):
            with patch.object(api_budget.subprocess, 'run', return_value=response):
                with self.assertRaises(RuntimeError):
                    api_budget.configure(True)

    def test_graphql_and_secondary_rate_limits_stop_the_scan(self):
        for message in ('API rate limit already exceeded', 'RATE_LIMITED',
                        'You have exceeded a secondary rate limit', 'Too Many Requests (HTTP 429)'):
            result = subprocess.CompletedProcess('gh', 1, '', message)
            with patch.object(api_budget.subprocess, 'run', return_value=result):
                with self.assertRaises(api_budget.Exhausted) as raised:
                    api_budget.run(['gh', 'api', 'graphql'])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(api_budget.defer(raised.exception), 1)

    def test_pr_text_does_not_turn_other_failures_into_rate_limits(self):
        for stdout in (
            json.dumps([{'body': 'API rate limit exceeded; RATE_LIMITED'}]),
            json.dumps({'message': 'API rate limit exceeded'}),
            json.dumps({'data': {'repository': {'pullRequest': {
                'body': 'secondary rate limit', 'message': 'RATE_LIMITED'}}}}),
            '[{"body":"RATE_LIMITED"}]\n[{"body":"api rate limit exceeded"}]',
        ):
            result = subprocess.CompletedProcess('gh', 1, stdout, 'gh: Bad Gateway (HTTP 502)')
            output = io.StringIO()
            with patch.object(api_budget.subprocess, 'run', return_value=result), \
                    contextlib.redirect_stdout(output):
                self.assertIs(api_budget.run(['gh', 'api', 'graphql']), result)
            self.assertFalse(api_budget.rate_limited)
            evidence = json.loads(output.getvalue())
            self.assertEqual(evidence['http_status'], 502)
            self.assertEqual(evidence['diagnostic'], 'Bad Gateway')
            self.assertIsNone(evidence['rate_limit_kind'])

    def test_api_errors_stop_without_searching_partial_data(self):
        for endpoint, code, response, stderr, kind in (
            ('repos/o/r/pulls', 1, {'message': 'API rate limit exceeded'},
             'gh: API rate limit exceeded (HTTP 403)', 'primary_or_unspecified'),
            ('graphql', 1, {'errors': [{'type': 'RATE_LIMITED'}]},
             'gh: API rate limit already exceeded for installation ID 1.', 'graphql'),
            ('graphql', 0, {'data': {'repository': None},
                            'errors': [{'type': 'RATE_LIMITED'}]}, '', 'graphql'),
            ('graphql', 1, {'errors': [{'message': 'You have exceeded a secondary rate limit'}]},
             'gh: You have exceeded a secondary rate limit (HTTP 403)', 'secondary'),
        ):
            with self.subTest(endpoint=endpoint, code=code):
                result = subprocess.CompletedProcess('gh', code, json.dumps(response), stderr)
                output = io.StringIO()
                with patch.object(api_budget.subprocess, 'run', return_value=result), \
                        contextlib.redirect_stdout(output), self.assertRaises(api_budget.Exhausted):
                    api_budget.run(['gh', 'api', endpoint])
                self.assertEqual(json.loads(output.getvalue())['rate_limit_kind'], kind)

    def test_non_api_cli_http_errors_and_later_page_rate_limits(self):
        for args, stdout, stderr, kind, status in (
            (['gh', 'pr', 'comment', '1'], '',
             'HTTP 429: Too Many Requests (https://api.github.com/repos/o/r/issues/1/comments)', 'http_429', 429),
            (['gh', 'label', 'create', 'label'], '',
             'HTTP 403: Resource not accessible by integration (https://api.github.com/repos/o/r/labels)', None, 403),
            (['gh', 'api', '--paginate', 'repos/o/r/pulls'], '[{"body":"RATE_LIMITED"}]\n',
             'gh: You have exceeded a secondary rate limit (HTTP 403)', 'secondary', 403),
        ):
            output = io.StringIO()
            result = subprocess.CompletedProcess('gh', 1, stdout, stderr)
            with patch.object(api_budget.subprocess, 'run', return_value=result), contextlib.redirect_stdout(output):
                if kind:
                    with self.assertRaises(api_budget.Exhausted):
                        api_budget.run(args)
                else:
                    self.assertIs(api_budget.run(args), result)
            evidence = json.loads(output.getvalue())
            self.assertEqual(evidence['http_status'], status)
            self.assertEqual(evidence['rate_limit_kind'], kind)

    def test_error_diagnostics_omit_fields_headers_queries_and_response_text(self):
        secret = 'SECRET_SENTINEL'
        args = ['gh', 'api', '--method', 'POST', '--header', 'repos/secret/header',
                '--header', 'Authorization: Bearer ' + secret, '--paginate', '--slurp',
                '/repos/o/r/pulls?token=' + secret, '-f', 'body=' + secret]
        result = subprocess.CompletedProcess('gh', 1,
                    json.dumps({'message': 'secondary rate limit ' + secret}),
                    'gh: secondary rate limit ' + secret + ' (HTTP 403)')
        output = io.StringIO()
        with patch.object(api_budget.subprocess, 'run', return_value=result), \
                contextlib.redirect_stdout(output), self.assertRaises(api_budget.Exhausted):
            api_budget.run(args)
        self.assertNotIn(secret, output.getvalue())
        evidence = json.loads(output.getvalue())
        self.assertEqual(evidence['request'], 'api repos/o/r/pulls')
        self.assertEqual(evidence['http_status'], 403)
        self.assertEqual(evidence['rate_limit_kind'], 'secondary')

    def test_successful_payload_with_rate_limit_words_is_unchanged_and_silent(self):
        result = subprocess.CompletedProcess('gh', 0,
            json.dumps({'message': 'API rate limit exceeded', 'body': 'RATE_LIMITED'}), '')
        output = io.StringIO()
        with patch.object(api_budget.subprocess, 'run', return_value=result), contextlib.redirect_stdout(output):
            self.assertIs(api_budget.run(['gh', 'api', 'repos/o/r/issues/1']), result)
        self.assertEqual(output.getvalue(), '')

    def test_active_unsafe_approval_is_withdrawn_before_rebase_wait_and_safe_paused_is_untouched(self):
        h, mb = 'a' * 40, 'b' * 40
        for unsafe, paused in ((True, False), (False, True)):
            with self.subTest(unsafe=unsafe, paused=paused):
                view = {'headRefOid': h, 'baseRefName': 'main', 'baseRefOid': mb,
                        'id': 'PR1', 'labels': [{'name': 'needs-rebase'}]}
                def read(args):
                    if args[:2] == ['pr', 'view']:
                        return view
                    if '/compare/' in args[1]:
                        return {'merge_base_commit': {'sha': mb}}
                    raise AssertionError(args)
                with patch.object(sweep, 'REPO', 'o/r'), patch.object(sweep, 'FOCUSED', True), \
                        patch.object(backend, 'selected', return_value={'backend': 'queue'}), \
                        patch.object(backend, 'github_entries', return_value=[{'number': 1}]), \
                        patch.object(sweep, 'bors_approved_prs', return_value=set()), \
                        patch.object(sweep, 'queue_entries', return_value=[{'number': 1}]), \
                        patch.object(sweep, 'open_prs', return_value=[{'number': 1, 'isDraft': paused}]), \
                        patch.object(backend, 'allow', return_value=True), \
                        patch.object(sweep, 'gh_json', side_effect=read), \
                        patch.object(sweep, 'gh_jsonl', return_value=[]), \
                        patch.object(sweep, 'pr_diff', return_value=['TauCeti/X.lean']), \
                        patch.object(sweep, 'decide_from_comments', return_value={'review_safe': not unsafe, 'merge': False}), \
                        patch.object(sweep, 'reconcile_rebase_request', return_value='waiting') as handoff, \
                        patch.object(sweep, 'withdraw_both', return_value=True) as revoke, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(sweep.main(), 0)
                handoff.assert_not_called()
                self.assertEqual(revoke.call_count, int(unsafe))

    def test_prior_action_failure_remains_red_when_the_next_pr_is_deferred(self):
        h, mb = 'a' * 40, 'b' * 40
        view = {'headRefOid': h, 'baseRefName': 'main', 'baseRefOid': mb, 'id': 'PR1'}
        with patch.object(api_budget, 'begin_pr', side_effect=[None, api_budget.Exhausted('budget')]), \
                patch.object(sweep, 'gh_json', return_value=view), \
                patch.object(sweep, 'gh_jsonl', return_value=[]), \
                patch.object(sweep, 'merge_base_now', return_value=mb), \
                patch.object(sweep, 'current_head', return_value=h), \
                patch.object(sweep, 'pr_diff', return_value=['TauCeti/X.lean']), \
                patch.object(sweep, 'decide_from_comments', return_value={'review_safe': False}), \
                patch.object(sweep, 'withdraw_both', return_value=False):
            with self.assertRaises(api_budget.Exhausted) as raised:
                sweep.sweep_bors([{'number': 1}, {'number': 2}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api_budget.defer(raised.exception), 1)

    def test_full_scan_queue_failure_includes_paused_prs_in_withdrawal_fallback(self):
        prs = [{'number': 1, 'isDraft': True},
               {'number': 2, 'labels': [{'name': 'keep'}]}]
        with patch.object(sweep, 'REPO', 'o/r'), patch.object(sweep, 'FOCUSED', False), \
                patch.object(backend, 'selected', return_value={'backend': 'queue'}), \
                patch.object(sweep, 'bors_approved_prs', return_value=set()), \
                patch.object(sweep, 'open_prs', return_value=prs), \
                patch.object(sweep, 'queue_entries', side_effect=RuntimeError('unavailable')), \
                patch.object(sweep, 'sweep_bors', return_value=0) as withdraw, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sweep.main(), 1)
        self.assertEqual({p['number'] for p in withdraw.call_args.args[0]}, {1, 2})
        self.assertFalse(withdraw.call_args.kwargs['admit'])

    def test_timeout_in_native_withdrawal_still_attempts_bors_revocation(self):
        with patch.object(api_budget.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gh', 30)), \
                patch.object(backend, 'publish_eligibility') as revoke, \
                patch.object(sweep, 'DRY_RUN', False), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(sweep.withdraw_both(1, 'PR1', 'a' * 40))
            revoke.assert_called_once()


if __name__ == '__main__':
    unittest.main()
