#!/usr/bin/env python3
"""Tau Ceti review runner (plumbing slice).

Runs one rubric with one model (claude or codex) over a checked-out PR, read-only, and
emits the structured verdict plus token usage. `--dry-run` neither posts nor updates a
ledger. This is the minimal end-to-end slice; the full runner (all rubrics, ledger,
posting, re-review) builds on it.
"""
import argparse, json, os, re, subprocess, sys, pathlib, random

CLAUDE_MODEL = "claude-sonnet-4-6"   # Sonnet-level default; override with --claude-model
CODEX_MODEL = ""                     # empty -> codex's configured default; override with --codex-model


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True,
                          stdin=subprocess.DEVNULL)


def build_prompt(rubrics_dir, rubric, context):
    common = (rubrics_dir / "_common.md").read_text()
    angle = (rubrics_dir / f"{rubric}.md").read_text()
    return (f"{common}\n\n---\n\n{angle}\n\n---\n\n# This pull request\n\n{context}\n\n"
            "Produce your review now. Output only the single JSON object specified above.")


def run_claude(prompt, cwd, model):
    r = sh(["claude", "-p", prompt, "--output-format", "json", "--model", model,
            "--allowedTools", "Read", "Grep", "Glob"], cwd=cwd)
    out = {"returncode": r.returncode, "raw_stderr": r.stderr[-4000:]}
    try:
        d = json.loads(r.stdout)
        out.update(text=d.get("result", ""), cost_usd=d.get("total_cost_usd"),
                   usage=d.get("usage"), session_id=d.get("session_id"),
                   is_error=d.get("is_error"))
    except Exception as e:
        out.update(text="", parse_error=str(e), raw_stdout=r.stdout[-4000:])
    return out


def run_codex(prompt, cwd, model):
    cmd = ["codex", "exec", "--json", "-s", "read-only"]
    if model:
        cmd += ["-m", model]
    cmd += [prompt]
    r = sh(cmd, cwd=cwd)
    out = {"returncode": r.returncode, "raw_stderr": r.stderr[-4000:]}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rubric", required=True)
    ap.add_argument("--provider", choices=["claude", "codex", "random"], default="random")
    ap.add_argument("--rubrics-dir", required=True)
    ap.add_argument("--tool-cwd", required=True, help="working directory for the reviewer's tools")
    ap.add_argument("--code-path", default="code", help="path to the code checkout, relative to tool-cwd")
    ap.add_argument("--roadmap-path", default="roadmap", help="path to the roadmap checkout, relative to tool-cwd")
    ap.add_argument("--diff-file", required=True)
    ap.add_argument("--pr", default="0")
    ap.add_argument("--round", default="1")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--claude-model", default=CLAUDE_MODEL)
    ap.add_argument("--codex-model", default=CODEX_MODEL)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    provider = a.provider if a.provider != "random" else random.choice(["claude", "codex"])
    rubrics_dir = pathlib.Path(a.rubrics_dir)
    diff = pathlib.Path(a.diff_file).read_text()[:120000]
    context = (f"This is PR #{a.pr} on FormalFrontier/TauCeti.\n"
               f"The code at the PR head is checked out at ./{a.code_path} and the roadmap repo at "
               f"./{a.roadmap_path}, relative to your working directory. Use your read-only tools "
               f"(Read/Grep/Glob) to inspect them.\n\n## Diff\n```diff\n{diff}\n```")
    prompt = build_prompt(rubrics_dir, a.rubric, context)

    runner = run_claude if provider == "claude" else run_codex
    model = a.claude_model if provider == "claude" else a.codex_model
    res = runner(prompt, a.tool_cwd, model)
    res.update(provider=provider, model=model or "(default)", rubric=a.rubric,
               verdict_obj=extract_verdict(res.get("text", "")))

    outdir = pathlib.Path(a.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{a.rubric}.json").write_text(json.dumps(res, indent=2))
    (outdir / f"{a.rubric}.prompt.txt").write_text(prompt)

    v = res["verdict_obj"] or {}
    print(f"[{a.rubric}] provider={provider} model={model or '(default)'} rc={res['returncode']} "
          f"verdict={v.get('verdict', 'PARSE_FAILED')} cost_usd={res.get('cost_usd')} usage={res.get('usage')}")
    if not res["verdict_obj"]:
        print("WARNING: no verdict JSON parsed. First 1500 chars of model text:", file=sys.stderr)
        print((res.get("text") or "")[:1500], file=sys.stderr)
        if res.get("raw_stderr"):
            print("--- stderr tail ---\n" + res["raw_stderr"][-1500:], file=sys.stderr)


if __name__ == "__main__":
    main()
