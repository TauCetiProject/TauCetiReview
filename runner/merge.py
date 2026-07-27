"""tauceti-review merge — split from review.py (behaviour-preserving).

Run as a script (runner/ on sys.path), so imports are flat siblings, not package-relative."""

import re


# REST/webhook payloads use the first form; GitHub's GraphQL `PullRequest.author.login` uses the
# second for the same App. Both are server-authenticated identities, not author-controlled text.
TRUSTED_LAKEFILE_AUTHORS = {"tauceti-review-bot[bot]", "app/tauceti-review-bot"}
TRUSTED_AUTHOR_ALLOW = {author: {"lakefile.toml"} for author in TRUSTED_LAKEFILE_AUTHORS}


def changed_paths(diff_text):
    """Repo-relative paths touched by a unified diff (both sides, to catch renames/deletes)."""
    paths = set()
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+)$", diff_text, flags=re.M):
        paths.add(m.group(1)); paths.add(m.group(2))
    return paths



def decide_merge(states, candidates, all_green, paths, head, prefix, allow, bump_guard, ci_build,
                 pr_author="", scope=""):
    """The single auto-merge rule, shared by the post-review path and the no-dispatch merge mode.

    Mergeable iff there is a head commit, CI's `build` check is green on it (so the merge path is
    self-sufficient and safe to run on comment events, not only after a successful build), every
    blocking rubric is green on it (fresh, not stale), and every changed path is under `prefix` or an
    allowed root file (`allow`), and CI's trusted scope status is green. A PR opened by the trusted
    Tau Ceti review bot may additionally change lakefile.toml, which is how the incompatibility
    tracker pins an exact first-known-bad Mathlib commit for repair. A PR touching any Lake pin,
    including that bot-only lakefile change, additionally requires the bump-guard check to be green
    (a CI-validated forward-only bump).
    Returns (merge_ok, reason)."""
    allow = set(allow or []) | TRUSTED_AUTHOR_ALLOW.get(pr_author, set())
    # Lake pins may auto-merge only as a CI-validated forward bump. lakefile.toml reaches this rule
    # only through the exact trusted-author exception above.
    pin_files = {"lake-manifest.json", "lean-toolchain", "lakefile.toml"}
    touches_pin = bool(paths & pin_files)
    code_only = bool(paths) and all(
        p.startswith(prefix) or p in allow for p in paths)
    if not head:
        return False, "no head_sha; refusing to merge"
    if (ci_build or "").lower() != "success":
        return False, f"build is not green on HEAD (={ci_build or 'missing'}); refusing to merge"
    if not all_green:
        return False, f"not all rubrics green on HEAD: {[r for r in candidates if states[r] != 'green']}"
    if (scope or "").upper() != "SUCCESS":
        return False, (f"trusted CI scope status is not green on HEAD (={scope or 'missing'}); "
                       f"refusing to merge")
    if not code_only:
        return False, (f"PR touches paths outside {prefix} "
                       f"(allowed extras: {sorted(allow)}); needs human merge")
    if touches_pin and (bump_guard or "").upper() != "SUCCESS":
        return False, (f"PR changes a Lake pin but bump-guard is not green "
                       f"(={bump_guard or 'missing'}); needs human merge")
    return True, f"all rubrics green on {head[:7]}; {prefix}+root only"
