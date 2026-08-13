#!/usr/bin/env python3
"""A provider outage ends the round; it does not become a review.

On 2026-08-13 an OAuth token was revoked mid-round. `correctness` had already approved; `reuse`
failed; then eight more rubrics failed five seconds apart, each recording the CLI's own
"Failed to authenticate. API Error: 401 OAuth access token has been revoked." as its result. The
round rendered a scoreboard headed "changes requested" with nine `⚠️ error` rows and POSTED it, and
an errored rubric blocks the merge. Across the stores this had happened 639 times, 107 of them
whole rounds, and the commonest text was not a status code at all but
"You've hit your session limit · resets 9:30pm (UTC)".

Two things were missing and are pinned here:

  1. error_kind only read stderr. Claude Code in -p mode puts a total provider failure in the
     RESULT text with an empty stderr, so every one of those classified as `unknown_error`. It now
     also reads a result short enough to be the whole diagnosis, and knows the subscription CLIs'
     prose for an exhausted plan.
  2. Nothing stopped the round. Two consecutive provider-down rubrics now abort it before anything
     is rendered or posted, with a distinct exit status the CLI reports as itself.

A real review that merely quotes "401" or "rate limit" must NOT be classified as an outage, and a
rubric that produced a verdict must reset the streak: both are asserted below.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import review  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def res(text="", stderr="", rc=1):
    return {"returncode": rc, "text": text, "raw_stderr": stderr}


def main():
    # --- 1) classification --------------------------------------------------------------------
    observed = {
        "Failed to authenticate. API Error: 401 OAuth access token has been revoked.": "not_authenticated",
        "Failed to authenticate. API Error: 401 Invalid authentication credentials": "not_authenticated",
        "You've hit your session limit · resets 9:30pm (UTC)": "quota_exhausted",
        "You've hit your weekly limit · resets 3am (UTC)": "quota_exhausted",
        "You've hit your monthly spend limit · raise it at claude.ai/settings/usage": "quota_exhausted",
    }
    for text, want in observed.items():
        got = review.error_kind(res(text=text))
        check(f"{want}: {text[:46]!r}", got == want)
        check(f"{want} counts as provider-down", want in review.PROVIDER_DOWN_KINDS)

    # stderr classification still works, and still wins nothing it did not win before.
    check("stderr is still read", review.error_kind(res(stderr="Not logged in · Please run /login"))
          == "not_authenticated")
    check("an unclassifiable failure is unknown_error", review.error_kind(res(text="???")) == "unknown_error")
    check("a clean exit with no verdict is no_verdict", review.error_kind(res(rc=0)) == "no_verdict")

    # A REAL review that discusses a 401 or a rate limit is not an outage. This is what the
    # short-text bound buys: a genuine review runs to many lines.
    long_review = "\n".join(
        ["The handler returns 401 when the token is invalid, which the docstring calls unauthorized."]
        + [f"line {i}: rate limit handling looks correct" for i in range(20)]
    )
    check("a long review quoting 401 is not misread", review.error_kind(res(text=long_review, rc=0))
          == "no_verdict")

    # --- 2) transient kinds do NOT trip the breaker -------------------------------------------
    for kind in ("overloaded", "timed_out", "transport", "model_unavailable", "unknown_error", "no_verdict"):
        check(f"{kind} does not trip the breaker", kind not in review.PROVIDER_DOWN_KINDS)

    # --- 3) the streak ------------------------------------------------------------------------
    ctx = review.RunContext(
        a=None, state_map={}, reply_text="", base_context="", head="deadbeef", providers=[],
        runners={}, keys={}, subscription=True, rubrics_version="v", round_num=1, prov={},
        diff_full="", outdir=None, day="2026-08-13", ledger=None, spent_today=0.0,
    )
    check("a fresh round is not down", not ctx.provider_is_down())
    ctx.provider_down_streak = 1
    check("one failure is not enough (a rotating token is a blip)", not ctx.provider_is_down())
    ctx.provider_down_streak = review.PROVIDER_DOWN_LIMIT
    check("two in a row is down", ctx.provider_is_down())
    check("the limit is 2", review.PROVIDER_DOWN_LIMIT == 2)

    # --- 4) run_rubric maintains the streak ----------------------------------------------------
    # The bookkeeping lives at the tail of run_rubric; assert the shape rather than re-running a
    # whole dispatch: a verdict resets, a provider-down kind increments, anything else resets.
    src = pathlib.Path(review.__file__).read_text()
    tail = src[src.index("def run_rubric("):]
    check("a verdict resets the streak", re.search(r"else:\s*\n\s*ctx\.provider_down_streak = 0", tail))
    check("a provider-down kind increments", "ctx.provider_down_streak += 1" in tail)
    check("another failure resets", tail.count("ctx.provider_down_streak = 0") >= 2)

    # --- 5) the abort publishes nothing --------------------------------------------------------
    abort = src[src.index("def abort_provider_down("):src.index("def stderr_summary(")]
    for forbidden in ("render_scoreboard", "post_plan", "scoreboard_file", "render_thread_plan"):
        check(f"abort does not {forbidden}", forbidden not in abort)
    check("abort clears the publication write-ahead marker",
          'pop("pending_publication_head_sha"' in abort)
    check("abort exits with the distinct status", "sys.exit(PROVIDER_DOWN_EXIT)" in abort)
    # Every provider-down kind needs a phrase: the abort's last line is what a driving worker reads
    # to classify the failure, and a KeyError there would replace the diagnosis with a traceback.
    for kind in review.PROVIDER_DOWN_KINDS | {"provider_unavailable"}:
        check(f"{kind} has an operator phrase", kind in review._PROVIDER_DOWN_PHRASE)
    check("the auth phrase classifies as an auth failure",
          "authentication" in review._PROVIDER_DOWN_PHRASE["not_authenticated"])

    # --- 6) the phase loops consult it, and the CLI agrees on the status ------------------------
    calls = re.findall(r"(?<!def )abort_provider_down\(ctx\)", src)  # exclude the definition itself
    check("both phase loops break out", len(calls) == 2)
    cli = (pathlib.Path(review.__file__).parent / "cli.py").read_text()
    m = re.search(r"^PROVIDER_DOWN_EXIT = (\d+)$", cli, re.M)
    check("cli.py defines the same exit status", bool(m) and int(m.group(1)) == review.PROVIDER_DOWN_EXIT)
    check("cli.py exits on it rather than dying generically",
          "if r.returncode == PROVIDER_DOWN_EXIT:" in cli)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
