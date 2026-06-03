#!/usr/bin/env python3
"""Tau Ceti review runner.

Runs the review rubrics over a checked-out PR with agentic CLIs (claude / codex, chosen at
random per rubric), read-only, then posts an aggregated verdict to the PR and records token
spend. A daily USD budget halts spending; partial review is a first-class outcome.

The workflow provides: a checkout of the review repo (rubrics + this runner), a checkout of
the TauCeti code at the PR head under ./code, a checkout of the roadmap under ./roadmap, the
PR diff, and an app token in GH_TOKEN for posting. Persisting the ledger/logs is the
workflow's job (it commits the out-dir); the runner just writes files and updates the ledger.
"""
import argparse, datetime, json, os, pathlib, random, re, subprocess, sys

DEFAULT_RUBRICS = ["scope", "correctness", "reuse", "proof-quality"]
CLAUDE_MODEL = "claude-sonnet-4-6"
CODEX_MODEL = "gpt-5.5"   # Sonnet-level analogue; override with --codex-model

# Rough USD per 1M tokens (input, output), for the daily-budget guard. claude reports exact
# cost; codex does not, so we estimate from these. Keep conservative.
PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "gpt-5.5": (1.25, 10.0),
}
DEFAULT_PRICE = (3.0, 15.0)


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, stdin=subprocess.DEVNULL)


def build_prompt(rubrics_dir, rubric, context):
    common = (rubrics_dir / "_common.md").read_text()
    angle = (rubrics_dir / f"{rubric}.md").read_text()
    return (f"{common}\n\n---\n\n{angle}\n\n---\n\n# This pull request\n\n{context}\n\n"
            "Produce your review now. Output only the single JSON object specified above.")


def run_claude(prompt, cwd, model):
    r = sh(["claude", "-p", prompt, "--output-format", "json", "--model", model,
            "--allowedTools", "Read", "Grep", "Glob"], cwd=cwd)
    out = {"returncode": r.returncode, "raw_stderr": r.stderr[-3000:]}
    try:
        d = json.loads(r.stdout)
        out.update(text=d.get("result", ""), cost_usd=d.get("total_cost_usd"),
                   usage=d.get("usage"), session_id=d.get("session_id"))
    except Exception as e:
        out.update(text="", parse_error=str(e), raw_stdout=r.stdout[-3000:])
    return out


def run_codex(prompt, cwd, model):
    cmd = ["codex", "exec", "--json", "-s", "read-only"]
    if model:
        cmd += ["-m", model]
    cmd += [prompt]
    r = sh(cmd, cwd=cwd)
    out = {"returncode": r.returncode, "raw_stderr": r.stderr[-3000:]}
    text, usage, thread = "", None, None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if t == "thread.started":
            thread = ev.get("thread_id")
        elif t == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
            text = ev["item"].get("text", "")
        elif t == "turn.completed":
            usage = ev.get("usage")
    out.update(text=text, usage=usage, session_id=thread)
    # estimate cost
    if usage:
        pin, pout = PRICES.get(model, DEFAULT_PRICE)
        cost = (usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout) / 1e6
        out["cost_usd"] = round(cost, 6)
        out["cost_estimated"] = True
    return out


def extract_verdict(text):
    cands = re.findall(r"\{.*?\}", text, flags=re.S) + re.findall(r"\{.*\}", text, flags=re.S)
    for cand in reversed(cands):
        try:
            d = json.loads(cand)
            if isinstance(d, dict) and "verdict" in d:
                return d
        except Exception:
            pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def load_ledger(path):
    p = pathlib.Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"days": {}, "prs": {}}


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def render_comment(results, overall):
    emoji = {"approve": "✅", "request_changes": "🟡", "block": "⛔", "error": "⚠️"}
    lines = [f"## AI review: **{overall}**", "",
             "_Automated, advisory. Each angle is judged by a separate model; only integrity "
             "angles can block. See the rubrics in TauCetiReview._", ""]
    for r in results:
        v = r.get("verdict_obj") or {}
        verdict = v.get("verdict", "error")
        lines.append(f"### {emoji.get(verdict,'•')} {r['rubric']} — {verdict}  "
                     f"`{r['provider']}/{r['model']}`")
        if v.get("summary"):
            lines.append(v["summary"])
        for f in (v.get("findings") or []):
            loc = f.get("file", "") + (f":{f['line']}" if f.get("line") else "")
            lines.append(f"- {('`'+loc+'` — ') if loc else ''}{f.get('issue','')}"
                         + (f" _Fix:_ {f['fix']}" if f.get("fix") else ""))
        if not v:
            lines.append(f"(no verdict parsed; rc={r.get('returncode')})")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="FormalFrontier/TauCeti")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--rubrics", default=",".join(DEFAULT_RUBRICS))
    ap.add_argument("--rubrics-dir", required=True)
    ap.add_argument("--tool-cwd", required=True)
    ap.add_argument("--code-path", default="code")
    ap.add_argument("--roadmap-path", default="roadmap")
    ap.add_argument("--diff-file", required=True)
    ap.add_argument("--round", default="1")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--daily-budget", type=float, default=5.0)
    ap.add_argument("--claude-model", default=CLAUDE_MODEL)
    ap.add_argument("--codex-model", default=CODEX_MODEL)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rubrics = [r.strip() for r in a.rubrics.split(",") if r.strip()]
    diff = pathlib.Path(a.diff_file).read_text()[:120000]
    context = (f"This is PR #{a.pr} on {a.repo}.\n"
               f"The code at the PR head is at ./{a.code_path} and the roadmap repo at "
               f"./{a.roadmap_path}, relative to your working directory; inspect them with "
               f"your read-only tools (Read/Grep/Glob).\n\n## Diff\n```diff\n{diff}\n```")

    ledger = load_ledger(a.ledger)
    day = today()
    spent_today = ledger["days"].get(day, 0.0)
    outdir = pathlib.Path(a.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    results, stopped = [], None
    for rubric in rubrics:
        if spent_today >= a.daily_budget:
            stopped = rubric
            break
        provider = random.choice(["claude", "codex"])
        model = a.claude_model if provider == "claude" else a.codex_model
        runner = run_claude if provider == "claude" else run_codex
        res = runner(prompt := build_prompt(pathlib.Path(a.rubrics_dir), rubric, context),
                     a.tool_cwd, model)
        res.update(provider=provider, model=model, rubric=rubric,
                   verdict_obj=extract_verdict(res.get("text", "")))
        cost = res.get("cost_usd") or 0.0
        spent_today += cost
        ledger["prs"].setdefault(str(a.pr), {"rounds": []})
        results.append(res)
        (outdir / f"{rubric}.json").write_text(json.dumps(res, indent=2))
        v = res["verdict_obj"] or {}
        print(f"[{rubric}] {provider}/{model} rc={res['returncode']} "
              f"verdict={v.get('verdict','PARSE_FAILED')} cost=${cost:.4f} today=${spent_today:.2f}")

    verdicts = [(r.get("verdict_obj") or {}).get("verdict") for r in results]
    overall = ("blocked" if "block" in verdicts
               else "changes requested" if "request_changes" in verdicts
               else "approved" if results and all(v == "approve" for v in verdicts)
               else "partial")
    if stopped:
        overall += f" (budget reached; skipped {stopped} and after)"

    comment = render_comment(results, overall)
    (outdir / "summary.md").write_text(comment)
    ledger["days"][day] = round(spent_today, 6)
    ledger["prs"][str(a.pr)]["rounds"].append(
        {"round": a.round, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "overall": overall, "cost": round(sum((r.get('cost_usd') or 0) for r in results), 6),
         "verdicts": {r["rubric"]: (r.get("verdict_obj") or {}).get("verdict") for r in results}})

    print(f"\nOVERALL: {overall}  (round cost ${sum((r.get('cost_usd') or 0) for r in results):.2f}, "
          f"today ${spent_today:.2f}/{a.daily_budget})")

    if a.dry_run:
        print("[dry-run] not posting; not updating ledger on disk.")
        return
    pathlib.Path(a.ledger).write_text(json.dumps(ledger, indent=2))
    r = sh(["gh", "pr", "comment", a.pr, "--repo", a.repo, "--body", comment])
    print("posted comment" if r.returncode == 0 else f"POST FAILED: {r.stderr[-500:]}")


if __name__ == "__main__":
    main()
