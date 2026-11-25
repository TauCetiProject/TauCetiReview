#!/usr/bin/env python3
"""An unpaid engine deferral cannot masquerade as a review or hide stale output."""
import json
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runner"))
import cli


def main():
    with tempfile.TemporaryDirectory() as directory:
        work = pathlib.Path(directory)
        head = "a" * 40
        path = work / "review-deferred.json"
        assert cli.budget_deferred(work, head) is False
        valid = {"schema": "tauceti.review-deferred/v1", "head_sha": head, "reason": "daily-budget"}
        path.write_text(json.dumps(valid))
        assert cli.budget_deferred(work, head) is True
        for invalid in ({**valid, "head_sha": "b" * 40}, {**valid, "reason": "provider"}, {}, []):
            path.write_text(json.dumps(invalid))
            with patch.object(cli, "die", side_effect=RuntimeError("refused")):
                try:
                    cli.budget_deferred(work, head)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("untrusted marker accepted")
        path.write_text(json.dumps(valid))
        for name in ("scoreboard.md", "post_plan.json"):
            output = work / name
            output.write_text("stale output must not be posted")
            with patch.object(cli, "die", side_effect=RuntimeError("refused")):
                try:
                    cli.budget_deferred(work, head)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("deferral accepted with conflicting review output")
            output.unlink()
    print("budget deferral: valid unpaid marker only; stale/foreign/output conflicts refused")


if __name__ == "__main__":
    main()
