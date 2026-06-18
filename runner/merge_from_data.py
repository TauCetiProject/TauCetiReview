#!/usr/bin/env python3
"""Compute the auto-merge decision from TauCetiData round records (the live verdict source).

The merge gate's source of truth is the per-PR round records archived in TauCetiData
(`records/rounds/<pr>/<round_id>.json`, schema `tauceti.round/v1`). Each carries the PR, the exact
`head_sha` it reviewed, the per-rubric `states`, and an `overall`. A PR is mergeable when its LATEST
live, production, exact round record AT THE CURRENT HEAD has EVERY required rubric green (and its own
`overall` agrees) — head-pinned by construction, so no separate stale-approval check — and the shared
`decide_merge` rule holds (build green, TauCeti/-only + allowed root/pins, bump-guard for a pin).

TauCetiData is a public, write-open store, so a record is untrusted input: every field is validated,
the full required rubric set must be present and green (a partial or forged record missing rubrics is
absent → not green), and a malformed record is skipped rather than crashing the gate. (The open store
still means a determined actor can forge a *complete* approval; the hard boundary remains CI build +
scope + bump-guard, and attribution is the planned follow-up.) Writes `merge.json` like
`review.py --mode merge`, so the caller is unchanged downstream of the decision.

    merge_from_data.py --pr 183 --head-sha <sha> --rounds-dir <dir> --rubrics-dir rubrics \
        --diff-file diff.txt --ci-build SUCCESS --bump-guard SUCCESS --merge-decision-file merge.json
"""
import argparse
import json
import pathlib

from review import DEFAULT_RUBRICS, changed_paths, decide_merge


def latest_round_at_head(rounds_dir, pr, head_sha):
    """The newest live, production, exact round record that reviewed exactly `head_sha`.

    Every field is validated because records come from a public, write-open store; a record that is
    malformed, the wrong schema/pr/head, not live/production/exact, or has a non-dict `states` or a
    non-int `round` is skipped rather than trusted or crashing the caller. Head-pinning is intrinsic:
    only a record whose own `head_sha` equals the PR's current head is eligible.
    """
    best_key, best = None, None
    for p in sorted(pathlib.Path(rounds_dir).glob("*.json")):
        try:
            d = json.loads(p.read_text())
            if not isinstance(d, dict):
                continue
            if d.get("schema") != "tauceti.round/v1":
                continue
            if str(d.get("pr")) != str(pr) or d.get("head_sha") != head_sha:
                continue
            if d.get("source") != "live" or d.get("arm") != "production" or d.get("fidelity") != "exact":
                continue
            if not isinstance(d.get("states"), dict):
                continue
            rnd = d.get("round")
            if not isinstance(rnd, int):
                continue
            ts = d.get("ts") if isinstance(d.get("ts"), str) else ""
        except (OSError, json.JSONDecodeError):
            continue
        key = (rnd, ts)
        if best_key is None or key > best_key:
            best_key, best = key, d
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--rounds-dir", required=True, help="dir of TauCetiData round records for this PR")
    ap.add_argument("--rubrics", default=",".join(DEFAULT_RUBRICS),
                    help="comma list of rubrics that must ALL be green to merge (default: DEFAULT_RUBRICS)")
    ap.add_argument("--diff-file", required=True)
    ap.add_argument("--ci-build", default="")
    ap.add_argument("--bump-guard", default="")
    ap.add_argument("--merge-path-prefix", default="TauCeti/")
    # Mirror review.py: the root aggregator plus the two machine-validated Lake pins.
    ap.add_argument("--merge-allow-file", action="append",
                    default=["TauCeti.lean", "lake-manifest.json", "lean-toolchain"])
    ap.add_argument("--merge-decision-file", default="")
    a = ap.parse_args()

    required = {r for r in a.rubrics.split(",") if r}
    if not required:
        merge_ok, reason = False, "no rubric set; refusing to merge"
    else:
        rrec = latest_round_at_head(a.rounds_dir, a.pr, a.head_sha)
        if rrec is None:
            merge_ok, reason = False, f"no live production review at head {a.head_sha[:7]}"
        else:
            # Required rubrics must ALL be present and green; a missing one is `absent` (not green),
            # so a partial or forged record can't satisfy the rule. The record's own `overall` must
            # also say approved — a belt-and-suspenders check on top of recomputing the rule.
            states = {r: (rrec["states"].get(r) or "absent") for r in required}
            candidates = sorted(required)
            all_green = (all(states[r] == "green" for r in required)
                         and rrec.get("overall") == "approved")
            paths = changed_paths(pathlib.Path(a.diff_file).read_text())
            merge_ok, reason = decide_merge(
                states, candidates, all_green, paths, a.head_sha,
                a.merge_path_prefix, a.merge_allow_file, a.bump_guard, a.ci_build)
            if merge_ok and rrec.get("overall") != "approved":
                merge_ok, reason = False, "round record overall is not approved; refusing"

    out = {"merge": merge_ok, "reason": reason, "head_sha": a.head_sha}
    print(json.dumps(out))
    if a.merge_decision_file:
        pathlib.Path(a.merge_decision_file).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
