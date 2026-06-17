# Review-cost analytics (`tauceti-review-costs`)

Attribute the review engine's spend — **tokens *and* imputed dollars, tracked
separately** — to the PRs it reviewed and to the lines of code that merged. It
reads the engine's own store (the same `--store` directory the reviewer and
`archive.py` use), joins each round to its PR's outcome and size via `gh`, and
keeps the result in SQLite. Stdlib-only, like the rest of the engine.

It answers:

- **token costs** — input / cached / output / reasoning, and tokens per merged LOC
- **imputed dollars** — $ per merged LOC, $ wasted on PRs later closed unmerged, $/day & /week
- both split by the **agent that authored** the PR (e.g. codex vs claude)

## Sources

| source | tokens | $ | when |
|--------|:------:|:-:|------|
| **data** a [TauCetiData](https://github.com/FormalFrontier/TauCetiData) checkout | ✅ | ✅ | **canonical** — durable, public, reproducible (`--source data --data-dir …`) |
| **store** `~/.cache/tauceti-review/store/<repo>/` | ✅ | ✅ | the live engine cache — fast, local, single-machine |
| **logs** `task-*.log` | ❌ | ✅ | last-resort fallback, dollars only (`--source logs --logs-dir …`) |

The **data** source is the one to prefer: TauCetiData is the durable, public,
append-only system of record, so anyone can clone it and reproduce the same
numbers without access to a local cache. Each `records/runs/<pr>/<run_id>.json`
carries the full `usage` block, `started_at`, `model`, and the engine's
`cost_usd`. It defaults to the **production** arm (the reviews that actually
gated PRs); pass `--include-shadows` to also count the archived A/B experiment
arms (`shadow:*`). The same logical run can be archived from several backfill
sources, so runs are de-duplicated by their `dedupe_key` (which, unlike
`(pr,round,rubric)`, also distinguishes genuinely separate runs at different
commits/models).

The **store** is the live engine cache — convenient on the machine that ran the
reviews, but ephemeral and single-machine; use it for a quick local look. The
**logs** path only ever had dollar figures, no tokens. `--source auto` (default)
prefers `data` when `--data-dir` is given, else the store, else logs — never
mixing, so nothing is double-counted.

```bash
git clone --depth 1 https://github.com/FormalFrontier/TauCetiData /tmp/TauCetiData
tauceti-review-costs --source data --data-dir /tmp/TauCetiData all
```

### Pricing — cost is derived from tokens, priced as of each run's date

Cost is **not a stored fact** — it is `f(tokens, prices)`. The immutable fact is
the token count; the engine's recorded `cost_usd` was computed at review time with
whatever `prices.json` was live then (older records used stale rates and, before
the cache-aware fix, charged cache-read tokens at full input rate), so it can't be
trusted. This tool **recomputes** every *estimated* (codex/pi) cost from tokens,
using the same cache-aware formula `review.py` applies:

```
cost = ((input − cached)·input_rate + cached·cache_read + output·output_rate) / 1e6
```

escalating the whole request to the long-context tier when input crosses a model's
threshold (e.g. gpt-5.5 above 272K). Real provider-billed costs
(`cost_estimated: false` — e.g. the claude CLI's self-reported `total_cost_usd`)
are kept as recorded.

**Each run is priced as of its own date, not today's prices.** "What did this run
cost?" must use the rate in effect when it ran — repricing a May run at June rates
is wrong. The rates come from [`prices.json`](prices.json), which is a single
**dated** table (the *same* file the engine bills from): each model maps to windows
with an `effective` date. A genuine provider price change *adds* a dated window (old
runs keep the old rate); a correction of a wrong past table *edits* the window
covering the affected dates. The engine bills at the newest window; the analysis
prices each run at the window covering its run date. Because there is only one file,
there is nothing to drift against.

Three lenses, all derived, never stored as authoritative:

| Lens | Rates used | Answers |
|------|-----------|---------|
| **faithful** (`cost_usd`, the headline) | the window covering the run's date | "what did this run cost?" |
| **forecast** (`cost_today`) | the newest window | "what would this cost today?" |
| **as-recorded** (`cost_recorded`) | whatever the engine wrote then | "what did the budget gate see?" |

The report prints all three totals so drift between them is explicit, and warns on
stderr if a record's model is missing from `prices.json` (it falls back to
`DEFAULT_PRICE`). **Tokens are measured; dollars are imputed** — ~89% of rounds are
derived from the price table, and ~70% of input tokens are cache hits, so the
figure sits far below tokens×list-price.

## Usage

```bash
# installed console script (after `pip install -e .` / uvx), or `python3 -m runner.costs`
tauceti-review-costs all            # ingest + refresh PRs + report
tauceti-review-costs all --graph    # also write ~/.cache/tauceti-review/review-costs.svg

tauceti-review-costs ingest                 # store (or logs) -> DB
tauceti-review-costs prs                      # PR outcomes/LOC from GitHub (cached)
tauceti-review-costs report --window week     # day|week
tauceti-review-costs report --csv out.csv     # per-PR CSV (tokens + $)
tauceti-review-costs graph --out g.svg         # dependency-free SVG (4 panels)
```

Defaults: DB and graph live under `~/.cache/tauceti-review/`; `--repo` is
`FormalFrontier/TauCeti`; `--store` defaults to that repo's store slug. PR author
is read from the body trailer (`🤖 Prepared with Codex` / `Claude Code`), since
commits land under the contributor's account.

## Schema (for ad-hoc SQL)

- `rubric_runs(run_key, pr, round_no, rubric, provider, model, input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, cost_usd, cost_today, cost_recorded, cost_estimated, verdict, ts)` — finest grain; `run_key` is `pr:round:rubric` (store) or the run's `dedupe_key` (data); `cost_usd` = faithful (priced as of `ts`'s date), `cost_today` = forecast (today's prices), `cost_recorded` = engine's original
- `review_rounds(key, source, pr, round_no, ts, day, verdict, rubrics_run, input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, cost, est_frac)` — per-round aggregate
- `prs(pr, state, additions, deletions, created_at, merged_at, closed_at, title, author_agent, author_name, fetched_at)`

```bash
# most token-hungry rubrics
sqlite3 ~/.cache/tauceti-review/review-costs.db \
  "SELECT rubric, SUM(output_tokens) o FROM rubric_runs GROUP BY rubric ORDER BY o DESC;"
```
