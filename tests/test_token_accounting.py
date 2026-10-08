#!/usr/bin/env python3
"""Provider cache fields produce comparable totals in comments, ledgers, and cost reports."""

import json
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runner"))

from runner import costs  # noqa: E402
from pricing import sum_usage, usage_totals  # noqa: E402
from render import render_thread, run_meta  # noqa: E402


CLAUDE = {"input_tokens": 98, "cache_read_input_tokens": 171774,
          "cache_creation_input_tokens": 1000, "output_tokens": 13259}
CODEX = {"input_tokens": 400000, "cached_input_tokens": 300000,
         "cache_write_input_tokens": 1000, "output_tokens": 1200}
PI = {"input_tokens": 200, "cached_input_tokens": 800, "output_tokens": 50}


def test_provider_totals():
    assert usage_totals("claude", CLAUDE) == {
        "input_tokens": 172872, "fresh_input_tokens": 98,
        "cached_input_tokens": 171774, "cache_creation_input_tokens": 1000,
        "output_tokens": 13259, "reasoning_output_tokens": 0}
    assert usage_totals("sonnet", CLAUDE)["input_tokens"] == 172872
    assert usage_totals("codex", CODEX)["input_tokens"] == 400000
    assert usage_totals("codex", CODEX)["fresh_input_tokens"] == 99000
    assert usage_totals("deepseek", PI)["input_tokens"] == 1000
    assert usage_totals("claude", {"input_tokens": 0, "cache_read_input_tokens": 500})["input_tokens"] == 500


def test_comment_and_ledger():
    cf = {"rubric": "placement", "provider": "claude", "model": "claude-fable-5-1",
          "usage": CLAUDE, "verdict": "request_changes"}
    body = render_thread(cf)
    assert "172.9k in (171.8k cache read, 1k cache write) / 13.3k out tokens" in body
    meta = json.loads(re.search(r"<!--tauceti-meta:v1 (.*?)-->", body).group(1))
    assert meta["runs"][0]["tok"] == {"in": 172872, "cin": 171774, "win": 1000, "out": 13259}
    assert run_meta({"provider": "codex", "usage": CODEX})["tok"]["in"] == 400000
    assert sum_usage([{"provider": "claude", "usage": CLAUDE},
                      {"provider": "codex", "usage": CODEX},
                      {"provider": "deepseek", "usage": PI}]) == {
        "input_tokens": 573872, "fresh_input_tokens": 99298,
        "cached_input_tokens": 472574, "cache_creation_input_tokens": 2000,
        "output_tokens": 14509, "reasoning_output_tokens": 0}


def test_cost_report_ingestion():
    with tempfile.TemporaryDirectory() as raw:
        root = pathlib.Path(raw)
        runs = root / "records" / "runs" / "42"
        runs.mkdir(parents=True)
        for name, provider, model, usage in (
            ("claude", "claude", "claude-fable-5-1", CLAUDE),
            ("codex", "codex", "gpt-5.6-sol", CODEX),
            ("pi", "deepseek", "deepseek/deepseek-v4-pro", PI),
        ):
            (runs / f"{name}.json").write_text(json.dumps({
                "pr": 42, "round": 1, "run_id": name, "provider": provider,
                "model": model, "usage": usage, "started_at": "2026-09-26T21:20:20Z",
                "cost_usd": 1.7, "cost_estimated": provider != "claude"}))
        con = costs.connect(root / "costs.db")
        try:
            assert costs.ingest_data(con, root) == (1, 3)
            rows = {r["provider"]: r for r in con.execute("SELECT * FROM rubric_runs")}
            assert (rows["claude"]["input_tokens"], rows["claude"]["cached_input_tokens"]) == (172872, 171774)
            assert rows["claude"]["cost_usd"] == 1.7  # provider-billed cost remains authoritative
            assert (rows["codex"]["input_tokens"], rows["codex"]["cached_input_tokens"]) == (400000, 300000)
            assert (rows["deepseek"]["input_tokens"], rows["deepseek"]["cached_input_tokens"]) == (1000, 800)
            assert con.execute("SELECT input_tokens FROM review_rounds").fetchone()[0] == 573872
        finally:
            con.close()


if __name__ == "__main__":
    for test in (test_provider_totals, test_comment_and_ledger, test_cost_report_ingestion):
        test()
        print(f"ok  {test.__name__}")
