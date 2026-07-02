"""tauceti-review render — split from review.py (behaviour-preserving).

Run as a script (runner/ on sys.path), so imports are flat siblings, not package-relative."""

import datetime, hashlib, json, pathlib, re

from pricing import fmt_tok
from verdict import newest_reply_id, state_of


def rubrics_fingerprint(rubrics_dir):
    """Short hash of all rubric text — including the references/ documents, which are part of a
    rubric's prompt (reviewers.RUBRIC_REFERENCES) — so a rubric or reference edit invalidates
    carried-forward approvals."""
    h = hashlib.sha256()
    d = pathlib.Path(rubrics_dir)
    for p in sorted(d.glob("*.md")) + sorted(d.glob("references/*.md")):
        h.update(p.relative_to(d).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]



def sanitize(text, limit=2000):
    """Model-derived text rendered into a comment body is untrusted: strip HTML comments so a
    prompt-injected reviewer cannot forge a `tauceti-meta`/`tauceti-rubric` marker, drop control
    characters, and cap the length. Applied at render time only — stored records keep the raw
    text."""
    if not text:
        return ""
    t = re.sub(r"<!--.*?(-->|$)", "", str(text), flags=re.S)
    t = "".join(ch for ch in t if ch == "\n" or ord(ch) >= 32)
    return t[:limit]



def meta_block(kind, **payload):
    """Hidden machine-readable provenance, the LAST line of every rendered body. Values come only
    from runner-verified inputs (sanitize() upstream keeps model text out of the comment-marker
    namespace); a scraper trusts the block only on the final line of a bot-authored comment."""
    obj = {"kind": kind}
    obj.update((k, v) for k, v in payload.items() if v not in (None, "", []))
    return "<!--tauceti-meta:v1 " + json.dumps(obj, separators=(",", ":"), sort_keys=True) + "-->"



def rubric_url(prov, rubric=None):
    """Link to the rubrics pinned at the exact commit reviewed from, falling back to main."""
    repo = (prov or {}).get("rubrics_repo", "TauCetiProject/TauCetiReview")
    sha = (prov or {}).get("rubrics_sha")
    if rubric:
        return f"https://github.com/{repo}/blob/{sha or 'main'}/rubrics/{rubric}.md"
    return f"https://github.com/{repo}/tree/{sha or 'main'}/rubrics"



def diff_url(prov):
    """The exact diff reviewed, as a three-dot compare (merge-base semantics — what `gh pr diff`
    produces); both endpoint SHAs stay visible in the URL."""
    if not (prov and prov.get("base_sha") and prov.get("head_sha")):
        return ""
    return (f"https://github.com/{prov['repo']}/compare/"
            f"{prov['base_sha']}...{prov['head_sha']}")



def run_meta(res):
    """The per-run slice of the meta block: runner-verified execution facts, no model text."""
    u = res.get("usage") or {}
    tok = {k: v for k, v in
           (("in", u.get("input_tokens")),
            ("cin", u.get("cached_input_tokens") or u.get("cache_read_input_tokens")),
            ("out", u.get("output_tokens"))) if v}
    v = res.get("verdict_obj") or {}
    return {k: val for k, val in
            (("id", res.get("run_id")), ("rubric", res.get("rubric")),
             ("provider", res.get("provider")), ("model", res.get("model")),
             ("verdict", v.get("verdict") or "error"), ("secs", res.get("duration_s")),
             ("tok", tok or None), ("usd", res.get("cost_usd")),
             ("est", res.get("cost_estimated") or None)) if val is not None}



def render_thread(cf, prov=None):
    """A blocking rubric's review-thread body. The hidden marker lets a reply map back to the
    rubric (Stage 2); the meta block at the end carries machine-readable provenance."""
    emoji = {"block": "⛔", "request_changes": "🟡", "error": "⚠️"}
    v = cf.get("verdict", "error")
    lines = [f"<!--tauceti-rubric:{cf['rubric']}-->",
             f"### {emoji.get(v, '•')} {cf['rubric']} — {v}  "
             f"`{cf.get('provider')}/{cf.get('model')}`", "", sanitize(cf.get("summary", "")), ""]
    for f in (cf.get("findings") or []):
        loc = sanitize((f.get("file") or "") + (f":{f['line']}" if f.get("line") else ""), 300)
        lines.append(f"- {('`' + loc + '` — ') if loc else ''}{sanitize(f.get('issue', ''))}"
                     + (f" _Fix:_ {sanitize(f['fix'])}" if f.get("fix") else ""))
    if not cf.get("findings"):
        lines.append("(no parseable verdict this round)")
    lines.append("\nReply in this thread to contest a finding; that re-runs **only** this rubric and "
                 "posts an answer here. (To fix it, just push a commit — that re-reviews on its own. "
                 "To contest again after an answer, post a NEW reply rather than editing an old one.)")
    sub = [f"`{cf.get('provider')}/{cf.get('model')}`"]
    if cf.get("duration_s"):
        sub.append(f"{cf['duration_s']:.0f}s")
    u = cf.get("usage") or {}
    if u.get("input_tokens") or u.get("output_tokens"):
        sub.append(f"{fmt_tok(u.get('input_tokens'))} in / {fmt_tok(u.get('output_tokens'))} out tokens")
    if diff_url(prov):
        sub.append(f"reviewing [this diff]({diff_url(prov)})")
    sub.append(f"[rubric]({rubric_url(prov, cf['rubric'])})")
    lines += ["", f"<sub>{' · '.join(sub)}</sub>", "",
              meta_block("thread", rubric=cf["rubric"], **thread_meta(cf, prov))]
    return "\n".join(lines)



def render_contest_reply(cf, head_sha, prov=None, answered_id=None):
    """A direct in-thread reply answering the author's contest: whether their reply cleared the
    finding, else the one-line reason it stands. The hidden `tauceti-reply:RUBRIC:through:<id>`
    marker carries the newest reply id answered THROUGH, so the post step never answers the same
    contest twice (and a later reply, with a higher id, is answered as a fresh contest)."""
    rubric = cf.get("rubric", "")
    aid = answered_id if answered_id is not None else newest_reply_id(cf)
    judge = f"{cf.get('provider')}/{cf.get('model')}" if cf.get("provider") else "—"
    if state_of(cf, head_sha) == "green":
        verdict = f"this clears the finding ✅ — approved on `{head_sha[:7]}`."
    else:
        why = sanitize((cf.get("summary") or "").replace("\n", " ")) or "the prior finding still holds"
        verdict = f"the finding stands — {why}"
    return (f"<!--tauceti-reply:{rubric}:through:{aid}-->\n"
            f"**Re: your reply on `{rubric}` —** re-reviewed on `{head_sha[:7]}`; {verdict}\n\n"
            f"<sub>`{judge}` · addresses your replies through comment {aid}.</sub>")



def thread_meta(cf, prov):
    """Provenance payload shared by a thread body and its 'now passing' close note."""
    return {**(prov or {}),
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "runs": [run_meta({**cf, "verdict_obj": {"verdict": cf.get("verdict")}})]}



def render_scoreboard(candidates, state_map, head_sha, overall, budget_note, cost_line="",
                      prov=None, runs=None):
    icon = {"green": "✅", "stale": "♻️", "blocking_request": "🟡", "blocking_block": "⛔",
            "error": "⚠️", "absent": "▫️"}
    word = {"green": "approved", "stale": "stale (re-run pending)",
            "blocking_request": "changes requested", "blocking_block": "blocked",
            "error": "error", "absent": "not yet run"}
    lines = ["<!--tauceti-scoreboard-->", f"## AI review — {overall}", "",
             "Each rubric is judged independently by multiple review agents; the PR merges only once "
             "**every** rubric is green — any rubric that is not green (changes requested, blocked, "
             f"errored, stale, or not yet run) blocks the merge. See the [rubrics]({rubric_url(prov)}).", "",
             "| | rubric | state | judge | summary |", "|---|---|---|---|---|"]
    for r in candidates:
        cf = state_map.get(r) or {}
        s = state_of(cf, head_sha)
        judge = f"{cf.get('provider')}/{cf.get('model')}" if cf.get("provider") else "—"
        summ = sanitize(cf.get("summary") or "").replace("\n", " ").replace("|", "\\|")
        name = f"[{r}]({rubric_url(prov, r)})" if (prov or {}).get("rubrics_sha") else r
        lines.append(f"| {icon[s]} | {name} | {word[s]} | `{judge}` | {summ} |")
    note = "♻️ = approved on an earlier commit, re-run before merge."
    lines += ["", f"{note}{(' ' + budget_note) if budget_note else ''}"]
    sub = []
    if diff_url(prov):
        sub.append(f"Reviewing [this diff]({diff_url(prov)}) at head `{head_sha[:7]}`")
    if (prov or {}).get("rubrics_sha"):
        sha = prov["rubrics_sha"]
        sub.append(f"rubrics @ [`{sha[:7]}`]({rubric_url(prov)})")
    if cost_line:
        sub.append(cost_line)
    if sub:
        lines += ["", f"<sub>{'. '.join(s.rstrip('.') for s in sub)}.</sub>"]
    lines += ["", meta_block(
        "scoreboard", **(prov or {}),
        ts=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Full per-rubric state at this head (not just this round's runs), so a reader (the merge gate)
        # has the complete verdict without re-deriving it from the rendered table. green == approved
        # at head_sha; anything else is not mergeable.
        states={r: state_of(state_map.get(r), head_sha) for r in candidates},
        # The highest author-reply comment id adjudicated across all rubrics. GitHub comment ids are
        # monotonic, so the worker can trigger a contest re-review precisely on `newest_reply_id >
        # replies_through` — second-resolution timestamps would conflate two replies in one second.
        replies_through=max((state_map.get(r, {}).get("last_reply_seen") or 0
                             for r in candidates), default=0),
        runs=[run_meta(r) for r in (runs or [])])]
    return "\n".join(lines)
