#!/usr/bin/env python3
"""Which rubric states spawn a contestable review thread.

Guards the codex-auth outage fallout: when the reviewer backend was down every rubric returned
`error` ("no parseable verdict"), and the engine posted one review thread per rubric per round —
hundreds of junk "reply to contest" comments on each PR. An `error` is an infrastructure failure,
not a finding, so it must NEVER spawn a thread; only genuine adverse verdicts do. Dependency-free:
run with `python tests/test_thread_states.py` or under pytest.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import review  # noqa: E402

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    fails += not ok

# Genuine adverse verdicts get a thread the author can contest.
check("block posts a thread",            review.posts_review_thread("blocking_block"),   True)
check("request_changes posts a thread",  review.posts_review_thread("blocking_request"), True)
# Infra error: blocking for merge, but NO thread (the bug we are fixing).
check("error posts NO thread",           review.posts_review_thread("error"),            False)
# Non-adverse states never post.
check("green posts no thread",           review.posts_review_thread("green"),            False)
check("stale posts no thread",           review.posts_review_thread("stale"),            False)
check("absent posts no thread",          review.posts_review_thread("absent"),           False)
# error must still BLOCK a merge (it just must not spawn a thread).
check("error is still blocking",         review.is_blocking("error"),                    True)

print("PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
