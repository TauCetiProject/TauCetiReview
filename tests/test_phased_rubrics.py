"""A rubric being phased in is run on, and required of, only PRs numbered above its cutoff.

PRs opened earlier keep their completed scoreboards: the merge gate does not ask them for the new
row, the handoff probe still calls their review complete, and the engine does not run the angle on
them. Reviewer subprocesses are stubbed, so there is no spend and no network.

Run: python tests/test_phased_rubrics.py
"""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tests"))
import admission_policy  # noqa: E402
import handoff  # noqa: E402
import merge_from_scoreboard as gate  # noqa: E402
import test_billing as tb  # noqa: E402  (the stubbed-reviewer harness)

REPO = "TauCetiProject/TauCeti"
FORGEJO = "forgejo:https://git.taucetiproject.org/taucetiproject/tauceti"  # as a review source names it
HEAD = "a" * 40
BASE = "b" * 40
TEN = list(handoff.DEFAULT_RUBRICS)
PHASED = "quality-pass"


def cutoffs(table):
    """Patch the phased rubric's cutoffs, in place, as the shipped constant is read."""
    return patch.dict(admission_policy.PHASED_RUBRICS, {PHASED: table})


def cutoff(value, repo=REPO):
    """Patch the phased rubric's cutoff for `repo`, the only repository it then has one for."""
    return cutoffs({repo: value})


def scoreboard(states):
    meta = {"head_sha": HEAD, "merge_base_sha": BASE, "mode": "commit", "states": states}
    return [{"body": f"<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 {json.dumps(meta)}-->",
             "updated_at": "2026-01-01T00:00:00Z"}]


def table_scoreboard(words):
    """A scoreboard from before `states` was recorded: its verdicts are only in the rendered table."""
    meta = {"head_sha": HEAD, "merge_base_sha": BASE, "mode": "commit"}
    rows = [f"| ✅ | [{rubric}](url) | {word} | `judge` | summary |" for rubric, word in words.items()]
    body = "\n".join(["<!--tauceti-scoreboard-->", "| | rubric | state | judge | summary |",
                      "|---|---|---|---|---|", *rows, f"<!--tauceti-meta:v1 {json.dumps(meta)}-->"])
    return [{"body": body, "updated_at": "2026-01-01T00:00:00Z"}]


TEN_GREEN = {r: "green" for r in TEN}
TEN_APPROVED = {r: "approved" for r in TEN}


class RubricsFor(unittest.TestCase):
    def test_an_angle_that_is_not_live_is_asked_of_no_pr(self):
        with cutoff(None):
            for pr in (1, 10 ** 9, "7"):
                self.assertEqual(handoff.rubrics_for(REPO, pr), TEN)

    def test_only_prs_above_the_cutoff_get_the_angle_and_it_runs_last(self):
        with cutoff(100):
            self.assertEqual(handoff.rubrics_for(REPO, 99), TEN)
            self.assertEqual(handoff.rubrics_for(REPO, 100), TEN)
            self.assertEqual(handoff.rubrics_for(REPO, 101), TEN + [PHASED])
            self.assertEqual(handoff.rubrics_for(REPO, "101"), TEN + [PHASED])

    def test_the_cutoff_belongs_to_one_repository(self):
        with cutoff(100):
            self.assertEqual(handoff.rubrics_for("Owner/Repo", 101), TEN)
            self.assertEqual(handoff.rubrics_for(REPO.lower(), 101), TEN + [PHASED])

    def test_an_unreadable_number_gets_the_default_angles(self):
        with cutoff(100):
            for pr in ("", "abc", None):
                self.assertEqual(handoff.rubrics_for(REPO, pr), TEN)

    def test_a_result_is_a_fresh_list(self):
        handoff.rubrics_for(REPO, 1).append("x")
        self.assertEqual(handoff.DEFAULT_RUBRICS, TEN)

    def test_each_forge_numbers_its_own_prs(self):
        with cutoffs({REPO: 100, FORGEJO: 3}):
            self.assertEqual(handoff.rubrics_for(FORGEJO, 3), TEN)
            self.assertEqual(handoff.rubrics_for(FORGEJO, 4), TEN + [PHASED])
            self.assertEqual(handoff.rubrics_for(REPO, 4), TEN)
            self.assertEqual(handoff.rubrics_for(REPO, 101), TEN + [PHASED])
        with cutoffs({REPO: 100, FORGEJO: None}):
            self.assertEqual(handoff.rubrics_for(FORGEJO, 101), TEN)

    def test_a_repository_has_one_name_however_it_is_spelled(self):
        with cutoff(100):
            for spelling in ("github:https://github.com/taucetiproject/tauceti", " TAUCETIPROJECT/tauceti "):
                self.assertEqual(handoff.rubrics_for(spelling, 101), TEN + [PHASED])
        with cutoff(3, repo="forgejo:https://git.example/Owner/Repo"):
            self.assertEqual(handoff.rubrics_for("forgejo:https://git.example/owner/repo", 4), TEN + [PHASED])
            self.assertEqual(handoff.rubrics_for("Owner/Repo", 4), TEN)  # the same name, on GitHub

    def test_a_source_binding_names_the_repository_under_review(self):
        binding = {"forge": {"kind": "forgejo", "url": "https://git.taucetiproject.org"},
                   "name": "TauCetiProject/TauCeti", "id": 1}
        with cutoffs({REPO: 100, FORGEJO: 3}):
            with patch.dict(os.environ, TAUCETI_SOURCE_BINDING=json.dumps(binding)):
                for spelling in (REPO, "https://git.taucetiproject.org/TauCetiProject/TauCeti", FORGEJO):
                    self.assertEqual(handoff.rubrics_for(spelling, 3), TEN)
                    self.assertEqual(handoff.rubrics_for(spelling, 4), TEN + [PHASED])
            for unreadable in ("not json", "{}", "[]"):
                with patch.dict(os.environ, TAUCETI_SOURCE_BINDING=unreadable):
                    self.assertEqual(handoff.rubrics_for(REPO, 4), TEN)
                    self.assertEqual(handoff.rubrics_for(FORGEJO, 4), TEN + [PHASED])

    def test_a_configured_rubric_list_is_phased_in_the_same_way(self):
        code = ("import admission_policy, handoff; "
                "admission_policy.PHASED_RUBRICS['quality-pass'] = {'Owner/Repo': 1}; "
                "print(handoff.rubrics_for('Owner/Repo', 1), handoff.rubrics_for('Owner/Repo', 5), "
                "handoff.rubrics_for('Other/Repo', 5), handoff.table_rubrics())")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "runner", check=True,
                             capture_output=True, text=True,
                             env={**os.environ, "TAUCETI_RUBRICS": '["custom"]'}).stdout
        self.assertEqual(out.strip(),
                         "['custom'] ['custom', 'quality-pass'] ['custom'] ['custom', 'quality-pass']")


class PolicyTable(unittest.TestCase):
    """The queue service reads the table through the pure policy module, with its own rubric list."""

    def test_the_phased_rubric_follows_the_list_the_caller_supplies(self):
        mine = ("correctness", "scope")
        with cutoff(100):
            self.assertEqual(admission_policy.with_phased_rubrics(mine, REPO, 100), list(mine))
            self.assertEqual(admission_policy.with_phased_rubrics(mine, REPO, 101), [*mine, PHASED])
            self.assertEqual(admission_policy.with_phased_rubrics(mine, "Owner/Repo", 101), list(mine))
        with cutoff(None):
            self.assertEqual(admission_policy.with_phased_rubrics(mine, REPO, 101), list(mine))

    def test_a_rubric_already_in_the_list_is_not_repeated(self):
        with cutoff(100):
            self.assertEqual(admission_policy.with_phased_rubrics([PHASED, "scope"], REPO, 101),
                             [PHASED, "scope"])

    def test_the_callers_list_is_left_as_it_was(self):
        mine = ["correctness"]
        with cutoff(100):
            admission_policy.with_phased_rubrics(mine, REPO, 101).append("x")
        self.assertEqual(mine, ["correctness"])

    def test_the_shipped_table_is_well_formed(self):
        for rubric, table in admission_policy.PHASED_RUBRICS.items():
            self.assertTrue((ROOT / "rubrics" / f"{rubric}.md").is_file(), rubric)
            keys = [admission_policy.repository_key(repo) for repo in table]
            self.assertEqual(len(set(keys)), len(keys), keys)
            for key, after in zip(keys, table.values()):
                self.assertRegex(key, r"^(github|forgejo):https://[^/\s]+/[^/\s]+/[^/\s]+$")
                self.assertTrue(after is None or (type(after) is int and after >= 0), after)

    def test_repositories_are_named_as_the_review_sources_name_them(self):
        from tauceti_tools.forge import GITHUB, Forge, RepositoryRef
        named = {admission_policy.repository_key(repo) for repo in admission_policy.PHASED_RUBRICS[PHASED]}
        tauceti = "TauCetiProject/TauCeti"
        self.assertEqual(admission_policy.repository_key(tauceti), RepositoryRef(GITHUB, tauceti).key)
        self.assertEqual(FORGEJO, RepositoryRef(Forge("forgejo", "https://git.taucetiproject.org"), tauceti).key)
        self.assertEqual(named, {RepositoryRef(GITHUB, tauceti).key, FORGEJO})

    def test_the_policy_module_stays_free_of_configuration(self):
        code = ("import sys; import admission_policy; "
                "print(sorted(m for m in ('handoff', 'ledger', 'tauceti_tools') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "runner", check=True,
                             capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "[]")


class MergeGate(unittest.TestCase):
    def decide(self, pr, states):
        return gate.decide_from_comments(
            scoreboard(states), HEAD, set(handoff.rubrics_for(REPO, pr)), {"TauCeti/Example.lean"},
            "SUCCESS", "", merge_path_prefix="TauCeti/", scope="SUCCESS", merge_base_sha=BASE)

    def test_a_completed_scoreboard_keeps_merging_while_the_angle_is_not_live(self):
        with cutoff(None):
            for pr in (5, 5000):
                self.assertTrue(self.decide(pr, TEN_GREEN)["merge"])

    def test_a_pr_opened_before_the_cutoff_is_not_asked_for_the_new_row(self):
        with cutoff(100):
            self.assertTrue(self.decide(100, TEN_GREEN)["merge"])

    def test_a_pr_above_the_cutoff_needs_the_new_row_green(self):
        with cutoff(100):
            missing = self.decide(101, TEN_GREEN)
            self.assertFalse(missing["merge"])
            self.assertFalse(missing["review_safe"])
            self.assertIn("not all-green", missing["reason"])
            self.assertFalse(self.decide(101, TEN_GREEN | {PHASED: "blocking_request"})["merge"])
            self.assertTrue(self.decide(101, TEN_GREEN | {PHASED: "green"})["merge"])

    def decide_table(self, pr, words):
        return gate.decide_from_comments(
            table_scoreboard(words), HEAD, set(handoff.rubrics_for(REPO, pr)), {"TauCeti/Example.lean"},
            "SUCCESS", "", merge_path_prefix="TauCeti/", scope="SUCCESS", merge_base_sha=BASE)

    def test_a_scoreboard_with_only_its_table_is_read_for_the_new_row(self):
        with cutoff(100):
            self.assertTrue(self.decide_table(100, TEN_APPROVED)["merge"])
            self.assertFalse(self.decide_table(101, TEN_APPROVED)["merge"])
            self.assertTrue(self.decide_table(101, TEN_APPROVED | {PHASED: "approved"})["merge"])
            self.assertFalse(self.decide_table(101, TEN_APPROVED | {PHASED: "changes requested"})["merge"])
            body = table_scoreboard(TEN_APPROVED | {PHASED: "changes requested"})[0]["body"]
            self.assertEqual(gate.states_from_table(body), TEN_GREEN | {PHASED: "blocking_request"})
        with cutoff(None):
            self.assertTrue(self.decide_table(101, TEN_APPROVED)["merge"])

    def test_a_table_is_not_read_for_a_rubric_the_project_does_not_have(self):
        # An explicit --rubrics selection decides what is required, not what a legacy table is read for.
        board = table_scoreboard(TEN_APPROVED | {"custom": "approved"})
        with cutoff(100):
            self.assertNotIn("custom", gate.states_from_table(board[0]["body"]))
            self.assertFalse(self.run_cli(100, None, "--rubrics", "custom", comments=board)["merge"])
            self.assertTrue(self.run_cli(100, None, "--rubrics", "scope", comments=board)["merge"])

    def run_cli(self, pr, states, *extra, comments=None):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "comments.json").write_text(json.dumps(comments or scoreboard(states)))
            (d / "paths.z").write_bytes(b"TauCeti/Example.lean\0")
            argv = ["merge_from_scoreboard", "--repo", REPO, "--pr", str(pr), "--head-sha", HEAD,
                    "--comments-file", str(d / "comments.json"), "--merge-base-sha", BASE,
                    "--paths-file", str(d / "paths.z"), "--ci-build", "SUCCESS",
                    "--scope", "SUCCESS", "--merge-path-prefix", "TauCeti/", *extra]
            out = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
                gate.main()
            return json.loads(out.getvalue())

    def test_the_command_takes_its_default_rubrics_from_the_pr(self):
        with cutoff(100):
            self.assertTrue(self.run_cli(100, TEN_GREEN)["merge"])
            self.assertFalse(self.run_cli(101, TEN_GREEN)["merge"])
            self.assertTrue(self.run_cli(101, TEN_GREEN | {PHASED: "green"})["merge"])

    def test_an_explicit_rubric_list_still_decides(self):
        with cutoff(100):
            self.assertTrue(self.run_cli(101, TEN_GREEN, "--rubrics", ",".join(TEN))["merge"])
            self.assertFalse(self.run_cli(100, TEN_GREEN, "--rubrics", ",".join(TEN + [PHASED]))["merge"])
            empty = self.run_cli(100, TEN_GREEN, "--rubrics", "")
            self.assertFalse(empty["merge"])
            self.assertIn("no rubric set", empty["reason"])


class HandoffProbe(unittest.TestCase):
    board = {"head_sha": HEAD, "states": TEN_GREEN, "replies_through": 0}

    def completed(self, pr):
        return handoff.completed_before_preparation(
            self.board, HEAD, handoff.rubrics_for(REPO, pr), {}, {})

    def test_a_ten_row_review_is_complete_only_where_ten_rows_are_asked(self):
        with cutoff(None):
            self.assertTrue(self.completed(101))
        with cutoff(100):
            self.assertTrue(self.completed(100))
            self.assertFalse(self.completed(101))

    def run_cli(self, pr, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "board.json").write_text(json.dumps(self.board))
            (d / "replies.json").write_text("{}")
            argv = ["handoff", "--repo", REPO, "--pr", str(pr), "--head-sha", HEAD, "--store", str(d),
                    "--board", str(d / "board.json"), "--replies", str(d / "replies.json"), *extra]
            out = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
                handoff.main()
            return json.loads(out.getvalue())["decision"]

    def test_the_command_takes_its_default_rubrics_from_the_pr(self):
        with cutoff(100):
            self.assertEqual(self.run_cli(100), "unchanged")
            self.assertEqual(self.run_cli(101), "continue")
            self.assertEqual(self.run_cli(101, "--rubrics", ",".join(TEN)), "unchanged")


class Engine(unittest.TestCase):
    """The engine, run without --rubrics, reviews a PR against the rubrics it is judged by."""

    def ran(self, pr, *extra):
        tb._install_stubs()
        tb._VERDICTS.clear()
        d, rubric_dir, store = tb._workspace(TEN + [PHASED])
        (rubric_dir / "references").mkdir()  # the naming rubric splices its reference document
        (rubric_dir / "references" / "naming-conventions.md").write_text("naming conventions\n")
        argv = ["review", "--pr", str(pr), "--repo", "Owner/Repo", "--rubrics-dir", str(rubric_dir),
                "--tool-cwd", str(d / "code"), "--diff-file", str(d / "diff.txt"),
                "--store", str(store), "--head-sha", HEAD, "--mode", "commit",
                "--auth", "subscription", "--no-post", "--providers", "claude",
                "--daily-budget", "5.0", "--max-call-cost", "1.0", *extra]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            tb.review.main()
        ledger = json.loads((store / "ledger.json").read_text())
        return ledger["prs"][str(pr)]["rounds"][-1]

    def test_the_angle_runs_last_and_only_above_the_cutoff(self):
        with cutoff(100, repo="Owner/Repo"):
            below = self.ran(100)
            self.assertEqual(below["ran"], TEN)
            self.assertEqual(below["states"], TEN_GREEN)
            above = self.ran(101)
            self.assertEqual(above["ran"], TEN + [PHASED])
            self.assertEqual(above["states"], TEN_GREEN | {PHASED: "green"})

    def test_the_angle_does_not_run_while_it_is_not_live(self):
        with cutoff(None, repo="Owner/Repo"):
            self.assertEqual(self.ran(101)["ran"], TEN)

    def test_an_explicit_rubric_list_still_decides(self):
        with cutoff(100, repo="Owner/Repo"):
            self.assertEqual(self.ran(100, "--rubrics", f"correctness,{PHASED}")["ran"],
                             ["correctness", PHASED])


class Sweep(unittest.TestCase):
    def test_every_sweep_gate_call_asks_for_the_prs_own_rubrics(self):
        source = (ROOT / "runner" / "sweep.py").read_text()
        self.assertNotIn("DEFAULT_RUBRICS", source)
        self.assertEqual(source.count("set(rubrics_for(REPO, n))"), 3)
        self.assertEqual(source.count("decide_from_comments(comments, head, "), 3)
        self.assertEqual(source.count("decide_from_comments(comments, head, set(rubrics_for(REPO, n)), ")
                         + source.count("decide_from_comments(comments, head, required, "), 3)


if __name__ == "__main__":
    unittest.main()
