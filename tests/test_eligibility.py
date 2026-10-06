import json
import pathlib
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runner"))
import eligibility as e


class EligibilityTests(unittest.TestCase):
    head = "a" * 40
    base = "b" * 40
    repo = "TauCetiProject/TauCeti"

    def live(self, **fields):
        return {"state": "open", "draft": False, "labels": [],
                "base": {"ref": "main", "sha": "c" * 40}, "head": {"sha": self.head}, **fields}

    def check(self, approve=True, **fields):
        return {"id": 10, "name": e.NAME, "app": {"id": e.APP_ID}, "head_sha": self.head,
                "status": "completed", "conclusion": "success" if approve is True else "failure" if approve is False else "neutral",
                "external_id": json.dumps(e.metadata(self.repo, 1, self.base, approve, False)), **fields}

    def run_publish(self, approve=True, checks=(), **options):
        self.posts = []
        def gh(args, payload=None):
            if payload:
                self.posts.append(payload)
                return {"id": 11}
            if "--slurp" in args:
                return [{"check_runs": list(checks), "total_count": len(checks)}]
            if "/compare/" in args[1]:
                return {"merge_base_commit": {"sha": self.base}}
            return self.live()
        e.publish(gh, Mock(), self.repo, 1, self.head, approve, merge_base=self.base, **options)
        return self.posts

    def test_admission_revocation_and_wait_are_distinct(self):
        for approve, conclusion, safe in [(True, "success", True), (False, "failure", False), (None, "neutral", True)]:
            p = self.run_publish(approve)[0]
            self.assertEqual(p["conclusion"], conclusion)
            self.assertEqual(p["head_sha"], self.head)
            self.assertEqual(json.loads(p["external_id"])["review_safe"], safe)

    def test_reconciliation_is_idempotent_and_ignores_forged_checks(self):
        self.assertEqual(self.run_publish(checks=[self.check()]), [])
        self.assertEqual(len(self.run_publish(checks=[self.check(app={"id": 99})])), 1)
        self.assertEqual(len(self.run_publish(checks=[self.check(False)])), 1)

    def test_newest_check_wins_over_older_green(self):
        self.assertEqual(len(self.run_publish(checks=[self.check(), self.check(False, id=12)])), 1)

    def test_single_is_part_of_the_decision(self):
        self.assertTrue(json.loads(self.run_publish(single=True)[0]["external_id"])["single"])

    def test_dry_run_never_publishes(self):
        self.assertEqual(self.run_publish(dry_run=True), [])

    def test_head_change_during_pagination_prevents_publication(self):
        live = self.live()
        changed = self.live(head={"sha": "d" * 40})
        gh = Mock(side_effect=[live, {"merge_base_commit": {"sha": self.base}},
                               [{"check_runs": []}], changed])
        e.publish(gh, Mock(), self.repo, 1, self.head, True, merge_base=self.base)
        self.assertEqual(gh.call_count, 4)

    def test_merge_base_change_prevents_publication(self):
        gh = Mock(side_effect=[self.live(), {"merge_base_commit": {"sha": "d" * 40}}])
        e.publish(gh, Mock(), self.repo, 1, self.head, True, merge_base=self.base)
        self.assertEqual(gh.call_count, 2)

    def test_malformed_listing_fails_closed(self):
        gh = Mock(side_effect=[self.live(), {"merge_base_commit": {"sha": self.base}}, []])
        with self.assertRaises(RuntimeError):
            e.publish(gh, Mock(), self.repo, 1, self.head, True, merge_base=self.base)

    def test_eligible_requires_reviewed_merge_base(self):
        with self.assertRaises(ValueError):
            e.publish(Mock(), Mock(), self.repo, 1, self.head, True)


if __name__ == "__main__":
    unittest.main()
