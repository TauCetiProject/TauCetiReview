import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runner"))
import direct_merge


class DirectMergeTests(unittest.TestCase):
    def run_merge(self, *, head="a", merge_base="b", eligible=True, mergeable=True):
        current = {"state": "open", "draft": False, "head": {"sha": head},
                   "base": {"sha": "new-main", "ref": "main"}, "labels": [], "mergeable": mergeable}
        with patch.object(direct_merge.backend, "selected", return_value={"backend": "direct"}), \
                patch.object(direct_merge.backend, "gh_json", side_effect=[
                    current, {"merge_base_commit": {"sha": merge_base}, "behind_by": 42}, {"merged": True}]) as gh:
            direct_merge.merge("o/r", 1, {"merge": eligible, "review_safe": True, "head_sha": "a"}, "b")
            return gh.call_args_list

    def test_behind_main_merges_the_exact_reviewed_head(self):
        calls = self.run_merge()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1].args[1], {"sha": "a", "merge_method": "squash"})

    def test_changed_head_or_diff_and_conflicts_cannot_merge(self):
        for kwargs in ({"head": "new"}, {"merge_base": "changed"}, {"mergeable": False}, {"mergeable": None}):
            with self.subTest(**kwargs):
                calls = self.run_merge(**kwargs)
                self.assertFalse(any("PUT" in c.args[0] for c in calls))

    def test_ineligible_never_reads_or_mutates(self):
        self.assertEqual(self.run_merge(eligible=False), [])


if __name__ == "__main__":
    unittest.main()
