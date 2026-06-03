#!/usr/bin/env python3
"""Tau Ceti review runner.

Reviews a PR with agentic CLIs (claude / codex, random per rubric, read-only), posts an
aggregated verdict, and records spend. State lives in a `--store` directory (a checkout of
the `reviews` branch of TauCetiReview): `ledger.json` plus `reviews/<pr>/<round>/`. A daily
USD budget halts spending. With `--auto-subset`, a re-review runs only the rubrics whose last
round was not `approve`. The workflow commits the store after the run.
"""
import argparse, datetime, json, os, pathlib, random, re, subprocess, sys, tempfile

DEFAULT_RUBRICS = ["scope", "correctness", "reuse", "proof-quality"]
CLAUDE_MODEL = "claude-sonnet-4-6"
CODEX_MODEL = "gpt-5.5"
PRICES = {"claude-sonnet-4-6": (3.0, 15.0), "gpt-5.5": (1.25, 10.0)}
DEFAULT_PRICE = (3.0, 15.0)


def sh(cmd, cwd=None, env=None, stdin_text=None):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, env=env,
                          input=stdin_text,
                          stdin=(None if stdin_text is not None else subprocess.DEVNULL))


def reviewer_env(provider, keys):
    """A minimal, isolated environment for a reviewer subprocess.

    Each reviewer gets a fresh throwaway HOME and ONLY its own provider credential — never the
    other provider's key, never a GitHub token (the parent posts/pushes in separate tokenless-here
    steps). This isolation is load-bearing: with public transcripts and no redaction gate, a
    prompt-injected reviewer must have nothing worth leaking. The unguessable HOME/CODEX_HOME keeps
    each provider's credential out of the other's reach. Residual: a reviewer can still read its OWN
    key via /proc/self/environ (documented in I2/R6; needs a proxy or uid-separation to close).
    """
    # Not under /tmp: codex refuses to create helper binaries when CODEX_HOME is in /tmp.
    base = os.path.join(os.path.expanduser("~"), ".tauceti-rev")
    os.makedirs(base, exist_ok=True)
    home = tempfile.mkdtemp(prefix=f"rev-{provider}-", dir=base)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": home,
           "LANG": os.environ.get("LANG", "C.UTF-8"), "CI": "1"}
    if provider == "claude":
        env["ANTHROPIC_API_KEY"] = keys["anthropic"]
    else:
        codex_home = os.path.join(home, ".codex")
        os.makedirs(codex_home, exist_ok=True)  # codex requires CODEX_HOME to already exist
        env["CODEX_HOME"] = codex_home
        env["OPENAI_API_KEY"] = keys["openai"]
    return env


def build_prompt(rubrics_dir, rubric, context):
    common = (rubrics_dir / "_common.md").read_text()
    angle = (rubrics_dir / f"{rubric}.md").read_text()
    return (f"{common}\n\n---\n\n{angle}\n\n---\n\n# This pull request\n\n{context}\n\n"
            "Produce your review now. Output only the single JSON object specified above.")


def run_claude(prompt, cwd, model, env):
    r = sh(["claude", "-p", prompt, "--output-format", "json", "--model", model,
            "--allowedTools", "Read", "Grep", "Glob"], cwd=cwd, env=env)
    out = {"returncode": r.returncode, "raw_stderr": r.stderr[-3000:]}
    try:
        d = json.loads(r.stdout)
        out.update(text=d.get("result", ""), cost_usd=d.get("total_cost_usd"),
                   usage=d.get("usage"), session_id=d.get("session_id"))
    except Exception as e:
        out.update(text="", parse_error=str(e), raw_stdout=r.stdout[-3000:])
    return out


def run_codex(prompt, cwd, model, env):
    # Authenticate into this invocation's isolated CODEX_HOME so the credential is not shared.
    sh(["codex", "login", "--with-api-key"], env=env, stdin_text=env.get("OPENAI_API_KEY", ""))
    # inherit=none: codex's model-run shell commands get a clean env, not codex's own.
    cmd = (["codex", "exec", "--json", "-s", "read-only", "--skip-git-repo-check",
            "-c", "shell_environment_policy.inherit=none"]
           + (["-m", model] if model else []) + [prompt])
    r = sh(cmd, cwd=cwd, env=env)
    out = {"returncode": r.returncode, "raw_stderr": r.stderr[-3000:]}
    text, usage, thread, events, errors = "", None, None, [], []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        events.append(t)
        if t == "thread.started":
            thread = ev.get("thread_id")
        elif t == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
            text = ev["item"].get("text", "")
        elif t == "turn.completed":
            usage = ev.get("usage")
        elif t and ("error" in t or "failed" in t):
            errors.append(ev)
    out.update(text=text, usage=usage, session_id=thread)
    # Surface why codex produced no usable answer, so failures are diagnosable not silent.
    if r.returncode != 0 or not text:
        out.update(event_types=events, error_events=errors[:5], raw_stdout=r.stdout[-3000:])
    if usage:
        pin, pout = PRICES.get(model, DEFAULT_PRICE)
        out["cost_usd"] = round((usage.get("input_tokens", 0) * pin
                                 + usage.get("output_tokens", 0) * pout) / 1e6, 6)
        out["cost_estimated"] = True
    return out


def extract_verdict(text):
    for cand in reversed(re.findall(r"\{.*?\}", text, flags=re.S) + re.findall(r"\{.*\}", text, flags=re.S)):
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


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def render_comment(results, overall, round_num):
    emoji = {"approve": "✅", "request_changes": "🟡", "block": "⛔", "error": "⚠️"}
    lines = [f"## AI review (round {round_num}): **{overall}**", "",
             "See the [review rubrics](https://github.com/FormalFrontier/TauCetiReview/tree/main/rubrics).", ""]
    for r in results:
        v = r.get("verdict_obj") or {}
        verdict = v.get("verdict", "error")
        lines.append(f"### {emoji.get(verdict, '•')} {r['rubric']} — {verdict}  `{r['provider']}/{r['model']}`")
        if v.get("summary"):
            lines.append(v["summary"])
        for f in (v.get("findings") or []):
            loc = (f.get("file") or "") + (f":{f['line']}" if f.get("line") else "")
            lines.append(f"- {('`' + loc + '` — ') if loc else ''}{f.get('issue', '')}"
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
    ap.add_argument("--mathlib-path", default="")
    ap.add_argument("--lean-src", default="")
    ap.add_argument("--diff-file", required=True)
    ap.add_argument("--store", required=True, help="checkout of the reviews branch (ledger + logs)")
    ap.add_argument("--daily-budget", type=float, default=5.0)
    ap.add_argument("--claude-model", default=CLAUDE_MODEL)
    ap.add_argument("--codex-model", default=CODEX_MODEL)
    ap.add_argument("--auto-subset", action="store_true",
                    help="re-review only rubrics whose last round was not approve")
    ap.add_argument("--keys-dir", default="",
                    help="dir with files 'anthropic' and 'openai'; keys are passed only to the "
                         "matching reviewer subprocess and never kept in this process's env")
    ap.add_argument("--comment-file", default="",
                    help="write the rendered review comment here for a separate post step")
    ap.add_argument("--no-post", action="store_true",
                    help="do not post the comment (a later tokened step does); still writes ledger")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.keys_dir:
        kd = pathlib.Path(a.keys_dir)
        keys = {}
        for name in ("anthropic", "openai"):
            f = kd / name
            keys[name] = f.read_text().strip() if f.exists() else ""
            # Read into memory then remove from disk: neither key should sit on a filesystem a
            # reviewer can reach while it runs (a codex reviewer must not find the anthropic key).
            if f.exists():
                f.unlink()
    else:  # local/dev fallback: read from this process's env
        keys = {"anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
                "openai": os.environ.get("OPENAI_API_KEY", "")}

    store = pathlib.Path(a.store)
    ledger_path = store / "ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"days": {}, "prs": {}}
    pr_rounds = ledger["prs"].get(str(a.pr), {}).get("rounds", [])
    round_num = len(pr_rounds) + 1

    candidates = [r.strip() for r in a.rubrics.split(",") if r.strip()]
    if a.auto_subset and pr_rounds:
        last = pr_rounds[-1].get("verdicts", {})
        rubrics = [r for r in candidates if last.get(r) != "approve"]
    else:
        rubrics = candidates
    if not rubrics:
        print("nothing to re-review (all rubrics approved in the last round).")
        return

    diff = pathlib.Path(a.diff_file).read_text()[:120000]
    src = ""
    if a.mathlib_path:
        src += f"- Mathlib source: `./{a.mathlib_path}` (grep before claiming a declaration exists).\n"
    if a.lean_src:
        src += f"- Lean core/toolchain source: `{a.lean_src}`.\n"
    context = (f"This is PR #{a.pr} on {a.repo} (review round {round_num}).\n"
               f"The code at the PR head is at ./{a.code_path} and the roadmap repo at "
               f"./{a.roadmap_path}; inspect them with your read-only tools (Read/Grep/Glob).\n"
               + (("\nSources you can grep:\n" + src) if src else "")
               + f"\n## Diff\n```diff\n{diff}\n```")

    day = today()
    spent_today = ledger["days"].get(day, 0.0)
    outdir = store / "reviews" / str(a.pr) / str(round_num)
    outdir.mkdir(parents=True, exist_ok=True)

    runners = {"claude": (run_claude, a.claude_model), "codex": (run_codex, a.codex_model)}

    results, stopped = [], None
    for rubric in rubrics:
        if spent_today >= a.daily_budget:
            stopped = rubric
            break
        prompt = build_prompt(pathlib.Path(a.rubrics_dir), rubric, context)
        provider = random.choice(["claude", "codex"])
        fn, model = runners[provider]
        env = reviewer_env(provider, keys)
        res = fn(prompt, a.tool_cwd, model, env)
        if res["returncode"] != 0 or extract_verdict(res.get("text", "")) is None:
            res = fn(prompt, a.tool_cwd, model, reviewer_env(provider, keys))  # one retry, transient blip
        res.update(provider=provider, model=model, rubric=rubric,
                   verdict_obj=extract_verdict(res.get("text", "")))
        spent_today += res.get("cost_usd") or 0.0
        results.append(res)
        (outdir / f"{rubric}.json").write_text(json.dumps(res, indent=2))
        v = res["verdict_obj"] or {}
        print(f"[{rubric}] {provider}/{model} rc={res['returncode']} "
              f"verdict={v.get('verdict', 'PARSE_FAILED')} cost=${res.get('cost_usd') or 0:.4f} "
              f"today=${spent_today:.2f}")

    verdicts = {r["rubric"]: (r.get("verdict_obj") or {}).get("verdict") for r in results}
    vals = list(verdicts.values())
    overall = ("blocked" if "block" in vals else "changes requested" if "request_changes" in vals
               else "approved" if vals and all(v == "approve" for v in vals) else "partial")
    if stopped:
        overall += f" (daily budget reached; skipped {stopped} and after)"
    round_cost = round(sum((r.get("cost_usd") or 0) for r in results), 6)
    comment = render_comment(results, overall, round_num)
    (outdir / "summary.md").write_text(comment)
    if a.comment_file:
        pathlib.Path(a.comment_file).write_text(comment)
    ledger["days"][day] = round(spent_today, 6)
    ledger["prs"].setdefault(str(a.pr), {"rounds": []})["rounds"].append(
        {"round": round_num, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "overall": overall, "cost": round_cost, "verdicts": verdicts})
    print(f"\nROUND {round_num} OVERALL: {overall}  (cost ${round_cost:.2f}, today ${spent_today:.2f}/{a.daily_budget})")

    if a.dry_run:
        print("[dry-run] not posting, not writing ledger.")
        return
    ledger_path.write_text(json.dumps(ledger, indent=2))
    if a.no_post:
        print("[--no-post] comment written for a separate post step; ledger written.")
        return
    r = sh(["gh", "pr", "comment", a.pr, "--repo", a.repo, "--body", comment])
    print("posted comment" if r.returncode == 0 else f"POST FAILED: {r.stderr[-400:]}")


if __name__ == "__main__":
    main()
