import io
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runner"))
import backend


class BackendTests(unittest.TestCase):
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

    def test_lost_revocation_is_retried_from_observed_approval(self):
        h = "a" * 40
        live = {"state": "open", "base": {"ref": "main"}, "head": {"sha": h}}
        data = {"batches": [], "held": [{"pr": 1, "head_sha": h}], "outcomes": []}
        with patch.object(backend, "gh_json", return_value=live) as gh, \
                patch.object(backend, "bors_observation", return_value=data), \
                patch("sys.stdout", new_callable=io.StringIO):
            backend.bors_command("o/r", 1, h, False)
            self.assertEqual(gh.call_count, 2)
            self.assertIn("body=bors r- sha=" + h, gh.call_args.args[0])

    def test_a_backend_flip_during_observation_prevents_post(self):
        h = "a" * 40
        live = {"state": "open", "draft": False, "base": {"ref": "main"}, "head": {"sha": h}}
        with patch.object(backend, "gh_json", return_value=live) as gh, \
                patch.object(backend, "allow", side_effect=[True, False]), \
                patch.object(backend, "bors_observation", return_value={"batches": [], "held": [], "outcomes": []}):
            backend.bors_command("o/r", 1, h, True)
            self.assertEqual(gh.call_count, 1)  # only the live PR read; no command


if __name__ == "__main__":
    unittest.main()
