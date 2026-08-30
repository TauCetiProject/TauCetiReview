#!/usr/bin/env python3
"""The scoreboard identifies the community reviewer that published it for downstream affinity."""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import render  # noqa: E402


META_RE = re.compile(r"<!--tauceti-meta:v1 (.*?)-->", re.S)


def scoreboard_meta(submitted_by):
    body = render.render_scoreboard(
        [], {}, "a" * 40, "approved", "",
        prov={"repo": "r", "pr": 1, "submitted_by": submitted_by},
    )
    return json.loads(META_RE.findall(body)[-1])


def test_submitted_by_is_machine_readable():
    assert scoreboard_meta("community-reviewer")["submitted_by"] == "community-reviewer"


def test_empty_submitted_by_is_omitted():
    assert "submitted_by" not in scoreboard_meta("")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\nall {len(tests)} scoreboard metadata checks passed")
