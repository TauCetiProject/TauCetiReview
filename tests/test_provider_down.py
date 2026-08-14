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
     reads that text too — but only once the CLI itself reports the run as failed, because `text` is
     model output over an untrusted diff and a short review saying "the `/login` handler returns
     401" must never read as an auth outage.
  2. Nothing stopped the round. Two consecutive provider-down ATTEMPTS on every configured provider
     now abort it before anything is rendered or posted, with a distinct exit status the CLI reports
     as itself. Attempts rather than rubrics, so a one-rubric reply round can stop too; every
     provider rather than any, so one provider's quota running out is not an outage while another
     can still review.

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


def res(text="", stderr="", rc=1, is_error=None):
    """A run result. rc=1 is what the observed provider failures actually carried; a legitimate
    review that simply never emitted its marker exits 0 with is_error False."""
    return {"returncode": rc, "text": text, "raw_stderr": stderr, "is_error": is_error}


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

    # A REAL review that discusses a 401 or a rate limit is not an outage, however SHORT it is.
    # Length is not a trust boundary: `text` is model output over an untrusted diff, so the gate is
    # the CLI's own structured failure signal. These one-liners are exactly the shape that would
    # otherwise be refunded for ever instead of eventually charging a PR that cannot be reviewed.
    for prose in (
        "the `/login` handler returns 401 on an expired token; the fix is wrong",
        "rate limit handling is wrong",
        "this diff would let a caller exceed your spend limit",
        "unauthorized access is not checked",
    ):
        check(f"a clean-exit review is not an outage: {prose[:38]!r}",
              review.error_kind(res(text=prose, rc=0, is_error=False)) == "no_verdict")
    # ...but the same words DO classify once the CLI reports the run as failed.
    check("is_error promotes the text to a diagnosis",
          review.error_kind(res(text="You've hit your session limit", rc=0, is_error=True)) == "quota_exhausted")
    check("a codex structured status promotes it too",
          review.error_kind({"returncode": 0, "text": "429 rate limit", "error_status": 429}) == "rate_limited")

    # --- 2) transient kinds do NOT trip the breaker -------------------------------------------
    for kind in ("overloaded", "timed_out", "transport", "model_unavailable", "unknown_error", "no_verdict"):
        check(f"{kind} does not trip the breaker", kind not in review.PROVIDER_DOWN_KINDS)

    # --- 3) the streak: per provider, counted in attempts ---------------------------------------
    def ctx_for(*providers):
        return review.RunContext(
            a=None, state_map={}, reply_text="", base_context="", head="deadbeef",
            providers=list(providers), runners={}, keys={}, subscription=True, rubrics_version="v",
            round_num=1, prov={}, diff_full="", outdir=None, day="2026-08-13", ledger=None,
            spent_today=0.0,
        )

    check("the limit is 2", review.PROVIDER_DOWN_LIMIT == 2)
    ctx = ctx_for("claude")
    check("a fresh round is not down", not ctx.provider_is_down())
    ctx.note_provider_down("claude", "not_authenticated", 1)
    check("one attempt is not enough (a rotating token is a blip)", not ctx.provider_is_down())
    ctx.note_provider_down("claude", "not_authenticated", 1)
    check("two attempts in a row is down", ctx.provider_is_down())
    check("the abort names the agreed kind", ctx.down_kind() == "not_authenticated")

    # A single-rubric round (a reply or a contest) confirms twice WITHIN its one rubric, because
    # run_rubric retries. It must be able to stop, or it renders the error scoreboard this exists
    # to prevent.
    one = ctx_for("claude")
    one.note_provider_down("claude", "quota_exhausted", 2)  # both attempts of one rubric
    check("one rubric's two attempts trip the breaker", one.provider_is_down())

    # A verdict resets that provider.
    ctx.note_provider_down("claude", None, 0)
    check("a verdict resets the provider", not ctx.provider_is_down())

    # Two providers: one being down is not an outage while the other can still review.
    two = ctx_for("claude", "codex")
    two.note_provider_down("claude", "not_authenticated", 2)
    check("one of two providers down is not an outage", not two.provider_is_down())
    two.note_provider_down("codex", "rate_limited", 2)
    check("both providers down is an outage", two.provider_is_down())
    check("disagreeing kinds fall back to a generic phrase", two.down_kind() == "provider_unavailable")

    # --- 4) run_rubric maintains the streak ----------------------------------------------------
    # The bookkeeping lives at the tail of run_rubric; assert the shape rather than re-running a
    # whole dispatch: a verdict resets, a provider-down kind increments, anything else resets.
    src = pathlib.Path(review.__file__).read_text()
    tail = src[src.index("def run_rubric("):]
    check("a verdict resets the provider", "ctx.note_provider_down(provider, None, 0)" in tail)
    check("a failure is recorded per provider", "ctx.note_provider_down(provider, kind if kind" in tail)
    check("attempts are counted, not rubrics", 'a.get("error_kind") in PROVIDER_DOWN_KINDS' in tail)

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
