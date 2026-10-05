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
            self.assertEqual(set(withdrawn[:2]), {1, 2}, mode)

    def test_budget_deferral_is_not_swallowed_by_admission_or_revocation(self):
        with patch.object(backend, 'selected', side_effect=api_budget.Exhausted('limited')):
            with self.assertRaises(api_budget.Exhausted):
                backend.allow('o/r', 'bors')
        with patch.object(sweep, 'dequeue', return_value=True), \
                patch.object(backend, 'bors_command', side_effect=api_budget.Exhausted('limited')):
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
                patch.object(backend, 'bors_command') as revoke, \
                patch.object(sweep, 'DRY_RUN', False), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(sweep.withdraw_both(1, 'PR1', 'a' * 40))
            revoke.assert_called_once()


if __name__ == '__main__':
    unittest.main()
