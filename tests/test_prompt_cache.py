#!/usr/bin/env python3
"""The shared prefix of a review round's rubric prompts, and how run_claude marks it for caching.

A review round runs one `claude -p` per rubric over the same pull request. The pull request is most
of each prompt, so every rubric after the first should read it from the prompt cache rather than
pay to write it again. That needs two things: every rubric's prompt starts with the same text, and
that text ends at a cache breakpoint, which a single `-p` text block cannot carry.

Dependency-free: run with `python tests/test_prompt_cache.py` or under pytest.
"""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import reviewers  # noqa: E402

RUBRICS = ROOT / "rubrics"
CTX = "This is PR #1.\n## Diff\n```diff\n+theorem foo : True := trivial\n```"


def test_rubrics_share_a_prefix_that_holds_the_pull_request():
    prompts = [reviewers.build_prompt(RUBRICS, r, CTX, f"MARKER-{r}", f"\nreactivation for {r}\n")
               for r in ("correctness", "naming", "scope")]
    assert len({p.shared for p in prompts}) == 1
    shared = prompts[0].shared
    assert "# Review agents: shared protocol" in shared and CTX in shared
    for p in prompts:
        assert str(p) == p.shared + p.rubric
        # Everything rubric-specific is after the prefix.
        assert "MARKER-" not in p.shared and "MARKER-" in p.rubric
        assert "reactivation for" not in p.shared and "reactivation for" in p.rubric
    assert "# Naming and notation" in prompts[1].rubric


def test_claude_input_marks_the_shared_prefix():
    p = reviewers.build_prompt(RUBRICS, "naming", CTX, "MARKER")
    msg = json.loads(reviewers.claude_input(p))
    assert msg["type"] == "user" and msg["message"]["role"] == "user"
    first, second = msg["message"]["content"]
    assert first == {"type": "text", "text": p.shared, "cache_control": {"type": "ephemeral"}}
    assert second == {"type": "text", "text": p.rubric}


def test_claude_input_accepts_a_plain_prompt():
    msg = json.loads(reviewers.claude_input("just text"))
    assert msg["message"]["content"] == [{"type": "text", "text": "just text"}]


def _run_claude_input(honours_ttl):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    orig_sh, orig_check = reviewers.sh, reviewers._claude_honours_cache_ttl
    reviewers.sh = lambda argv, **kw: (seen.update(argv=list(argv), **kw), R)[1]
    reviewers._claude_honours_cache_ttl = lambda: honours_ttl
    try:
        p = reviewers.build_prompt(RUBRICS, "naming", CTX, "MARKER")
        reviewers.run_claude(p, ".", "claude-opus-5-5", {"HOME": "/h"})
    finally:
        reviewers.sh, reviewers._claude_honours_cache_ttl = orig_sh, orig_check
    return p, seen


def test_run_claude_streams_the_blocks_with_a_pinned_ttl():
    p, seen = _run_claude_input(True)
    argv = seen["argv"]
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert json.loads(seen["stdin_text"])["message"]["content"][0]["text"] == p.shared
    # The breakpoint above carries no ttl, so Claude Code's own must be 5m as well.
    assert seen["env"] == {"HOME": "/h", "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}


def test_an_older_claude_gets_one_uncached_block():
    # A CLI that ignores the TTL pin keeps 1h breakpoints, and the API rejects ours before them.
    p, seen = _run_claude_input(False)
    assert json.loads(seen["stdin_text"])["message"]["content"] == [{"type": "text", "text": str(p)}]


def test_version_check():
    import subprocess, types
    orig = reviewers.subprocess.run
    try:
        for out, rc, want in (("2.1.321 (Claude Code)\n", 0, True), ("2.1.242 (Claude Code)", 0, True),
                              ("2.1.241 (Claude Code)", 0, False), ("", 1, False), ("garbage", 0, False)):
            reviewers.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=out)
            reviewers._claude_honours_cache_ttl.cache_clear()
            assert reviewers._claude_honours_cache_ttl() is want, out

        def missing(*a, **k):
            raise FileNotFoundError("claude")
        reviewers.subprocess.run = missing
        reviewers._claude_honours_cache_ttl.cache_clear()
        assert reviewers._claude_honours_cache_ttl() is False
    finally:
        reviewers.subprocess.run = orig
        reviewers._claude_honours_cache_ttl.cache_clear()


def test_review_prompt_survives_copy_and_pickle():
    import copy, pickle
    p = reviewers.build_prompt(RUBRICS, "naming", CTX, "MARKER")
    for q in (copy.copy(p), copy.deepcopy(p), pickle.loads(pickle.dumps(p))):
        assert q == p and q.shared == p.shared and q.rubric == p.rubric


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
