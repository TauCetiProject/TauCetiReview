import io
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runner"))
import backend


class BackendTests(unittest.TestCase):
    def test_observation_timeout_and_invalid_json_use_the_failure_path(self):
        with patch.object(backend.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            with self.assertRaises(RuntimeError):
                backend.github_entries("o/r")
        with patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess("gh", 0, "invalid", "")):
            with self.assertRaises(RuntimeError):
                backend.github_entries("o/r")

    def test_authorized_absence_only_defaults_to_queue(self):
        with patch.object(backend, "gh_json", return_value=[{"variables": [], "total_count": 0}]):
            self.assertEqual(backend.selected("o/r")["backend"], "queue")
        for value in (None, [], [{"variables": [{"name": "MERGE_BACKEND", "value": "typo"}]}]):
            with patch.object(backend, "gh_json", return_value=value):
                with self.assertRaises(RuntimeError):
                    backend.selected("o/r")
        with patch.object(backend, "gh_json", side_effect=RuntimeError("403")):
            with self.assertRaises(RuntimeError):
                backend.selected("o/r")

    def test_busy_and_failed_observations_defer(self):
        with patch.object(backend, "selected", return_value={"backend": "bors", "updated_at": "now"}), \
                patch.object(backend, "github_entries", return_value=[{"number": 1}]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(backend.allow("o/r", "bors"))
        with patch.object(backend, "selected", side_effect=RuntimeError("403")), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(backend.allow("o/r", "queue"))

    def test_pagination_and_incomplete_graphql(self):
        def page(n, more, cursor):
            return {"data": {"repository": {"mergeQueue": {"entries": {
                "nodes": [{"enqueuedAt": "now", "pullRequest": {"number": n, "id": str(n), "headRefOid": "a" * 40}}],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}}
        with patch.object(backend, "gh_json", side_effect=[page(1, True, "next"), page(2, False, None)]):
            self.assertEqual([e["number"] for e in backend.github_entries("o/r")], [1, 2])
        with patch.object(backend, "gh_json", return_value={"data": {"repository": None}}):
            with self.assertRaises(RuntimeError):
                backend.github_entries("o/r")

    def test_head_bound_membership_and_terminal_failure(self):
        head = "a" * 40
        d = {"batches": [], "held": [], "outcomes": [{"pr": 1, "head_sha": head, "state": "error"}]}
        self.assertEqual(backend.bors_head_state(d, 1, head), "terminal")
        self.assertEqual(backend.bors_head_state(d, 1, "b" * 40), "absent")
        d["batches"] = [{"members": [{"pr": 1, "head_sha": head}]}]
        self.assertEqual(backend.bors_head_state(d, 1, head), "approved")
        d["batches"] = []
        d["held"] = [{"pr": 1, "head_sha": head}]
        self.assertEqual(backend.bors_head_state(d, 1, head), "approved")

    def test_policy_delivery_is_independent_of_backend_selection(self):
        with patch.object(backend.eligibility, "publish") as publish:
            backend.publish_eligibility("o/r", 1, "a" * 40, True, merge_base="b" * 40)
            self.assertEqual(publish.call_args.args[5], True)
            backend.publish_eligibility("o/r", 1, "a" * 40, False)
            self.assertEqual(publish.call_args.args[5], False)


if __name__ == "__main__":
    unittest.main()
