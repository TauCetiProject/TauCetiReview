#!/usr/bin/env python3
"""Approvals carry across commits that leave the PR's own change untouched.

A stacked PR whose parent squash-merges must take the new base (merge-from-base or rebase); its
head moves, its diff does not. Before this, every approval went stale and the freshness sweep
re-ran all of them — one full review per PR per parent landing. `patch_digest` identifies the
change independently of the commit, and `carry_forward` re-pins approvals made on the same digest
under the same rubric text. Dependency-free: run with `python tests/test_patch_digest.py`.
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
# The same change after a rebase: new blob ids and shifted hunk offsets, same section label.
REBASED = DIFF.replace("1111111..2222222", "3333333..4444444").replace("@@ -10,6 +10,7 @@", "@@ -42,6 +42,7 @@")

d = casefile.patch_digest(DIFF)
check("digest is versioned",                d.startswith(casefile.PATCH_DIGEST_VERSION + ":"), True)
check("bytes and str agree",                casefile.patch_digest(DIFF.encode()), d)
check("rebase keeps the digest",            casefile.patch_digest(REBASED), d)
def differs(name, text):
    check(name, casefile.patch_digest(text) == d, False)
differs("a changed line changes it",         DIFF.replace("trivial", "by trivial"))
differs("a changed context line changes it", DIFF.replace(" more context", " other context"))
differs("whitespace inside a line counts",   DIFF.replace("+theorem added", "+theorem  added"))
differs("the hunk section label counts",     DIFF.replace("@@ theorem before", "@@ theorem other"))
differs("the file mode counts",              DIFF.replace("2222222 100644", "2222222 100755"))
differs("line endings count",                DIFF.replace("\n", "\r\n"))
differs("the file name counts",              DIFF.replace("Foo.lean", "Bar.lean"))
check("empty diff has no digest",           casefile.patch_digest(""), None)
BINARY = "diff --git a/x.png b/x.png\nindex 1111111..2222222 100644\nBinary files a/x.png and b/x.png differ\n"
check("binary change has no digest",        casefile.patch_digest(BINARY), None)
check("binary patch has no digest",         casefile.patch_digest(BINARY.replace("Binary files a/x.png and b/x.png differ", "GIT binary patch\nliteral 3\n")), None)
# Content lines that happen to start like headers are prefixed and so are never normalised.
check("content lines are not headers",      casefile.patch_digest(DIFF.replace("+theorem added : True := trivial", "+index 1..2")) == casefile.patch_digest(DIFF.replace("+theorem added : True := trivial", "+index 3..4")), False)

RV = "rubrics-v1"
def approved(sha, digest, rv=RV):
    return casefile.update_case_file({}, "naming", {"verdict_obj": {"verdict": "approve"}}, sha, digest, rv)
def requested(sha, digest, rv=RV):
    return casefile.update_case_file({}, "reuse", {"verdict_obj": {"verdict": "request_changes"}}, sha, digest, rv)

# Approval at sha1; head moves to sha2 with the same patch: carried, green, provenance kept.
sm = {"naming": approved("sha1", d)}
check("stale before carry",                 state_of(sm["naming"], "sha2"), "stale")
check("carry reports the rubric",           casefile.carry_forward(sm, "sha2", d, RV), ["naming"])
check("green after carry",                  state_of(sm["naming"], "sha2"), "green")
check("origin recorded",                    sm["naming"]["carried_from_sha"], "sha1")
check("where the model ran is immutable",   sm["naming"]["reviewed_sha"], "sha1")
check("carry is idempotent",                casefile.carry_forward(sm, "sha2", d, RV), [])
# A second carry keeps the true origin, not the previous carried-to head.
casefile.carry_forward(sm, "sha3", d, RV)
check("second carry keeps the origin",      sm["naming"]["carried_from_sha"], "sha1")
check("green on the third head",            state_of(sm["naming"], "sha3"), "green")

# A different patch carries nothing; the approval stays stale and is swept as before.
sm = {"naming": approved("sha1", d)}
check("changed patch carries nothing",      casefile.carry_forward(sm, "sha2", casefile.patch_digest(REBASED.replace("trivial", "by trivial")), RV), [])
check("still stale",                        state_of(sm["naming"], "sha2"), "stale")

# Different rubric text: the approval was made against another rubric, so it does not carry.
sm = {"naming": approved("sha1", d)}
check("rubric change carries nothing",      casefile.carry_forward(sm, "sha2", d, "rubrics-v2"), [])

# No diff on this invocation (no digest) carries nothing.
sm = {"naming": approved("sha1", d)}
check("no digest carries nothing",          casefile.carry_forward(sm, "sha2", None, RV), [])

# Approvals recorded by the old engine (no digest, no rubric version) never match.
sm = {"naming": approved("sha1", None, None)}
check("undigested approval not carried",    casefile.carry_forward(sm, "sha2", d, RV), [])
check("undigested approval stays stale",    state_of(sm["naming"], "sha2"), "stale")

# A blocking verdict is never carried: it is re-judged on the new head as before, so a base change
# that fixes the finding is seen and its thread never claims a head it was not made on.
sm = {"reuse": requested("sha1", d)}
check("blocker is not carried",             casefile.carry_forward(sm, "sha2", d, RV), [])
check("blocker keeps its verdict",          state_of(sm["reuse"], "sha2"), "blocking_request")
check("blocker still judged at sha1",       sm["reuse"]["reviewed_sha"], "sha1")

# A fresh run on the new head drops the carried provenance.
sm = {"naming": approved("sha1", d)}
casefile.carry_forward(sm, "sha2", d, RV)
casefile.update_case_file(sm, "naming", {"verdict_obj": {"verdict": "approve"}}, "sha3", d, RV)
check("fresh run clears carried_from",      "carried_from_sha" in sm["naming"], False)

print("PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
