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


if __name__ == '__main__':
    unittest.main()
