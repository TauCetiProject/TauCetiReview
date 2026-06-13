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
| **store** `~/.cache/tauceti-review/store/<repo>/` | ✅ | ✅ | default — the live engine cache |
| **logs** `task-*.log` | ❌ | ✅ | fallback for a worker's logs, dollars only (`--source logs --logs-dir …`) |

The store is authoritative: each `reviews/<pr>/<round>/<rubric>.json` carries a
real `usage` block (`input_tokens`, `cached_input_tokens`, `output_tokens`,
`reasoning_output_tokens`) and `cost_usd`; `ledger.json` supplies per-round
timestamps. `--source auto` (default) uses the store if present, else the logs —
never both, so nothing is double-counted.

### Pricing — recomputed from `prices.json`

[`runner/prices.json`](prices.json) is the single source of truth, and this tool
**recomputes** every *estimated* cost from it rather than trusting the `cost_usd`
the engine recorded at review time. Older records were written with stale rates
(and, before the cache-aware fix, charged cache-read tokens at full input rate),
so the as-recorded numbers are internally inconsistent. Recomputing values the
*measured token counts* uniformly at current rates, using the same formula
`review.py` applies to estimated costs:

```
cost = ((input − cached)·input_rate + cached·cache_read + output·output_rate) / 1e6
```

Real provider-billed costs (`cost_estimated: false` — e.g. the claude CLI's
self-reported `total_cost_usd`) are kept as recorded. The report prints both the
recomputed and as-recorded totals so price drift is visible, and warns on stderr
if a record's model is missing from `prices.json` (it falls back to the engine's
`DEFAULT_PRICE`).

**Tokens are measured; dollars are an estimate** — ~89% of rounds are estimated
(codex/pi, derived from `prices.json`), and ~71% of input tokens are cache hits,
so the dollar figure sits far below tokens×list-price.

> The durable archive ([TauCetiData](https://github.com/FormalFrontier/TauCetiData))
> stores records in a different `records/runs/<pr>/<run_id>.json` layout; this tool
> reads the live engine store. A TauCetiData reader is a clean follow-up.

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

- `rubric_runs(pr, round_no, rubric, provider, model, input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, cost_usd, cost_recorded, cost_estimated, verdict, ts)` — finest grain (store only); `cost_usd` is recomputed from `prices.json` for estimated rows, `cost_recorded` is the engine's original
- `review_rounds(key, source, pr, round_no, ts, day, verdict, rubrics_run, input_tokens, cached_input_tokens, output_tokens, reasoning_tokens, cost, est_frac)` — per-round aggregate
- `prs(pr, state, additions, deletions, created_at, merged_at, closed_at, title, author_agent, author_name, fetched_at)`

```bash
# most token-hungry rubrics
sqlite3 ~/.cache/tauceti-review/review-costs.db \
  "SELECT rubric, SUM(output_tokens) o FROM rubric_runs GROUP BY rubric ORDER BY o DESC;"
```
