#!/usr/bin/env python3
"""Verdicts carry across commits that leave the PR's own change untouched.

A stacked PR whose parent squash-merges must take the new base (merge-from-base or rebase); its
head moves, its diff does not. Before this, every approval went stale and the freshness sweep
re-ran all of them — one full review per PR per parent landing. `patch_digest` identifies the
change independently of the commit, and `carry_forward` re-pins verdicts made on the same digest.
Dependency-free: run with `python tests/test_patch_digest.py` or under pytest.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import casefile  # noqa: E402
from verdict import state_of  # noqa: E402

fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    fails += not ok

DIFF = """diff --git a/Foo.lean b/Foo.lean
index 1111111..2222222 100644
--- a/Foo.lean
+++ b/Foo.lean
@@ -10,6 +10,7 @@ theorem before
 context
+theorem added : True := trivial
 more context
"""
# The same change after a rebase: different blob ids, shifted hunk offsets, new context label.
REBASED = DIFF.replace("1111111..2222222", "3333333..4444444") \
              .replace("@@ -10,6 +10,7 @@ theorem before", "@@ -42,6 +42,7 @@ theorem other")
CHANGED_LINE = DIFF.replace("trivial", "by trivial")
CHANGED_CONTEXT = DIFF.replace(" more context", " other context")
WHITESPACE = DIFF.replace("+theorem added", "+theorem  added")

d = casefile.patch_digest(DIFF)
check("digest is versioned",              d.startswith(casefile.PATCH_DIGEST_VERSION + ":"), True)
check("rebase keeps the digest",          casefile.patch_digest(REBASED), d)
check("a changed line changes it",        casefile.patch_digest(CHANGED_LINE) == d, False)
check("a changed context line changes it", casefile.patch_digest(CHANGED_CONTEXT) == d, False)
check("whitespace inside a line counts",  casefile.patch_digest(WHITESPACE) == d, False)
check("empty diff has no digest",         casefile.patch_digest(""), None)

def approved(sha, digest):
    return casefile.update_case_file({}, "naming", {"verdict_obj": {"verdict": "approve"}}, sha, digest)
def requested(sha, digest):
    return casefile.update_case_file({}, "reuse", {"verdict_obj": {"verdict": "request_changes"}}, sha, digest)

# Approval at sha1; head moves to sha2 with the same patch: carried, green, provenance kept.
sm = {"naming": approved("sha1", d)}
check("stale before carry",               state_of(sm["naming"], "sha2"), "stale")
check("carry reports the rubric",         casefile.carry_forward(sm, "sha2", d), ["naming"])
check("green after carry",                state_of(sm["naming"], "sha2"), "green")
check("origin recorded",                  sm["naming"]["carried_from_sha"], "sha1")
check("carry is idempotent",              casefile.carry_forward(sm, "sha2", d), [])

# A different patch carries nothing; the approval stays stale and is swept as before.
sm = {"naming": approved("sha1", d)}
check("changed patch carries nothing",    casefile.carry_forward(sm, "sha2", casefile.patch_digest(CHANGED_LINE)), [])
check("still stale",                      state_of(sm["naming"], "sha2"), "stale")

# No diff on this invocation (no digest) carries nothing.
sm = {"naming": approved("sha1", d)}
check("no digest carries nothing",        casefile.carry_forward(sm, "sha2", None), [])

# An approval recorded by the old engine (no digest) never matches.
sm = {"naming": approved("sha1", None)}
check("undigested approval not carried",  casefile.carry_forward(sm, "sha2", d), [])
check("undigested approval stays stale",  state_of(sm["naming"], "sha2"), "stale")

# A blocking verdict on the same patch is also "already judged": reviewed_sha moves, verdict kept.
sm = {"reuse": requested("sha1", d)}
casefile.carry_forward(sm, "sha2", d)
check("blocker keeps its verdict",        state_of(sm["reuse"], "sha2"), "blocking_request")
check("blocker judged at head",           sm["reuse"]["reviewed_sha"], "sha2")
check("blocker has no approved_sha",      sm["reuse"].get("approved_sha"), None)

# A fresh run on the new head drops the carried provenance.
sm = {"naming": approved("sha1", d)}
casefile.carry_forward(sm, "sha2", d)
casefile.update_case_file(sm, "naming", {"verdict_obj": {"verdict": "approve"}}, "sha3", d)
check("fresh run clears carried_from",    "carried_from_sha" in sm["naming"], False)

print("PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
