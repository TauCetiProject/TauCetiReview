"""The importable policy preserves the pinned production gate without running the reviewer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner import admission_policy as policy


class AdmissionPolicyTests(unittest.TestCase):
    def test_trusted_recovery_holds_keep_their_head_and_author_binding(self):
        head = "a" * 40
        for stalled, marker in ((False, "tauceti-rebase"), (True, "tauceti-merge-stalled")):
            body = f"Merge-queue recovery for head `{head}`: repair needed\n\n<!--{marker}:v1 {head}-->"
            for author in ("tauceti-review-bot[bot]", "tau-ceti-bors[bot]"):
                self.assertEqual(policy.rebase_request_heads([{"author": author, "body": body}], stalled=stalled), {head})
                self.assertEqual(policy.rebase_request_heads([{"user": {"login": author}, "body": body}], stalled=stalled), {head})
            self.assertEqual(policy.rebase_request_heads([{"author": "contributor", "body": body}], stalled=stalled), set())
            self.assertEqual(policy.rebase_request_heads([{"author": "tau-ceti-bors[bot]", "body": "prefix " + body}], stalled=stalled), set())

    def test_pinned_production_decisions(self):
        fixture = json.loads((ROOT / "tests/fixtures/admission-policy.json").read_text())
        self.assertIn("2b4a9560", fixture["source"])
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(policy.decide_from_comments(**(fixture["configuration"] | case["inputs"])),
                                 case["expected"])

    def test_package_import_does_not_bootstrap_or_import_the_reviewer(self):
        script = """
import subprocess, sys
def forbidden(*args, **kwargs):
    raise AssertionError('policy import attempted a subprocess')
subprocess.run = forbidden
subprocess.Popen = forbidden
from runner import admission_policy
assert not any(name in sys.modules for name in ('review', 'runner.review', 'reviewers', 'tauceti_tools'))
assert not admission_policy.decide_from_comments([], 'a' * 40, {'correctness'},
    {'TauCeti/A.lean'}, 'SUCCESS', '', scope='SUCCESS',
    merge_path_prefix='TauCeti/', merge_allow_file=[], table_rubrics=['correctness'])['merge']
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                env={**os.environ, "TAUCETI_CONF": "/nonexistent/policy-test.toml"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_rendered_review_accepts_explicit_project_rubrics(self):
        head, base = "a" * 40, "b" * 40
        meta = {"head_sha": head, "merge_base_sha": base, "mode": "commit"}
        board = "<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 " + json.dumps(meta) + "-->\n"
        board += "| 🟢 | [custom-rubric](url) | approved | judge | reason |"
        decision = policy.decide_from_comments([{"body": board}], head, {"custom-rubric"},
                                              {"Project/A.lean"}, "SUCCESS", "",
                                              merge_path_prefix="Project/", merge_allow_file=[],
                                              table_rubrics=["custom-rubric"],
                                              scope="SUCCESS", merge_base_sha=base)
        self.assertTrue(decision["review_safe"] and decision["merge"], decision)

    def test_cli_loads_configuration_before_rubric_and_path_defaults(self):
        """Both configuration entry paths must enforce extra required rubrics and custom roots."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / "tauceti.toml"
            conf.write_text('schema_version = 1\n[repositories]\ncontent = "Owner/Project"\n'
                            'roadmap = "Owner/Roadmap"\n[lean]\nroots = ["Project"]\n'
                            '[review]\nrubrics = ["security"]\n')
            comments = root / "comments.json"
            paths = root / "paths.z"
            paths.write_bytes(b"Project.lean\0")
            head, base = "a" * 40, "b" * 40
            fixture = json.loads((ROOT / "tests/fixtures/admission-policy.json").read_text())
            for selection in ("flag", "environment"):
                for state, expected in (("blocking_block", False), ("green", True)):
                    with self.subTest(selection=selection, security=state):
                        states = {r: "green" for r in fixture["configuration"]["table_rubrics"]}
                        states["security"] = state
                        meta = {"head_sha": head, "merge_base_sha": base, "states": states}
                        comments.write_text(json.dumps([{"body": "<!--tauceti-scoreboard-->\n"
                            "<!--tauceti-meta:v1 " + json.dumps(meta) + "-->"}]))
                        env = {k: v for k, v in os.environ.items() if not k.startswith("TAUCETI_")}
                        argv = [sys.executable, str(ROOT / "runner/merge_from_scoreboard.py"),
                                "--pr", "1", "--head-sha", head, "--merge-base-sha", base,
                                "--comments-file", str(comments), "--paths-file", str(paths),
                                "--ci-build", "SUCCESS", "--scope", "SUCCESS"]
                        if selection == "flag":
                            argv.extend(["--conf", str(conf)])
                        else:
                            env["TAUCETI_CONF"] = str(conf)
                        result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout)["merge"], expected, result.stdout)


if __name__ == "__main__":
    unittest.main()
