"""tauceti-review pricing — split from review.py (behaviour-preserving).

Run as a script (runner/ on sys.path), so imports are flat siblings, not package-relative."""

import hashlib, json, os, pathlib


CLAUDE_MODEL = "claude-opus-5-5"

CODEX_MODEL = "gpt-5.6-sol"

# When the default codex model isn't available to the account, run_one downgrades to this one mid-run
# rather than failing every codex rubric: Sol needs a paid ChatGPT tier, while Free/Go subscriptions
# get Terra. Always kept priced — dispatch_models() lists it and the price-sync test enforces coverage.
CODEX_FALLBACK_MODEL = "gpt-5.6-terra"

# Kiro is an explicit-only subscription reviewer. Keep its default exact so Kiro's
# auto router can never silently choose a weaker model; callers may pin another
# entitled Kiro model with --kiro-model.
KIRO_MODEL = "gpt-5.6-sol"

# OpenRouter models driven through the `pi` agent (badlogic/pi-mono): a third reviewer
# family alongside claude/codex, selectable as --providers/--reviewer deepseek|minimax.
# Pay-per-token, so they run only when explicitly named — never auto-drawn. Add a row here
# and the provider is usable with no other change. Ids are env-overridable; each is its
# provider's strongest agentic, tool-using model on OpenRouter. (DeepSeek-Prover-V2 /
# ByteDance Seed-Prover are whole-proof search systems, not tool-using agents, and aren't
# served on OpenRouter, so they cannot drive `pi`.)
# Ids are env-overridable; the worker (round.sh) overrides the *authoring* model with
# DEEPSEEK_MODEL / MINIMAX_MODEL, so accept those too (with a TAUCETI_-prefixed form taking
# precedence) — a single `DEEPSEEK_MODEL=…` then pins both authoring and review to one id.
OPENROUTER_MODELS = {
    "deepseek": (os.environ.get("TAUCETI_DEEPSEEK_MODEL") or os.environ.get("DEEPSEEK_MODEL")
                 or "deepseek/deepseek-v4-pro"),
    "minimax": (os.environ.get("TAUCETI_MINIMAX_MODEL") or os.environ.get("MINIMAX_MODEL")
                or "minimax/minimax-m3"),
    "grok": (os.environ.get("TAUCETI_GROK_MODEL") or os.environ.get("GROK_MODEL")
             or "x-ai/grok-4.3"),
}

# Model pricing is loaded from prices.json (the single source of truth — edit there, never here).
# It is a DATED table: each model maps to a list of rate windows. The engine bills at the *newest*
# window per model; the analysis (tauceti-review-costs) prices each past run at the window covering
# its run date. review.py runs from the engine checkout, so the file always sits beside it. The
# daily budget and every archived run's cost_usd derive from these rates.
PRICES_PATH = pathlib.Path(__file__).resolve().parent / "prices.json"


def load_price_windows():
    """The dated price table {model: [window, ...]} straight from prices.json. Shared by the engine
    (newest window per model) and the cost analytics (tauceti-review-costs, dated per run)."""
    return json.loads(PRICES_PATH.read_text()).get("models", {})


_PRICE_WINDOWS = load_price_windows()



def _current_window(windows):
    return max(windows, key=lambda w: w["effective"])



_PRICES_NOW = {m: _current_window(ws) for m, ws in _PRICE_WINDOWS.items()}

PRICES = {m: (p["input"], p["output"]) for m, p in _PRICES_NOW.items()}

CACHE_READ = {m: p.get("cache_read", p["input"]) for m, p in _PRICES_NOW.items()}

DEFAULT_PRICE = (3.0, 15.0)  # last-resort fallback; require_priced() makes it unreachable in practice

# The exact prices.json that produced this run's costs — stamped onto every archived run so a
# stored cost_usd is auditable and its staleness is detectable (rates change; the tokens don't).
PRICES_SHA = hashlib.sha256(PRICES_PATH.read_bytes()).hexdigest()[:12]

# Sonnet is a cheaper claude-family A/B arm (the claude CLI pinned to Sonnet); a named constant so
# the price-coverage guard and its test see the same id the dispatcher uses.
SONNET_MODEL = "claude-sonnet-4-6"



def dispatch_models(claude_model=CLAUDE_MODEL, codex_model=CODEX_MODEL):
    """Every model id the engine can dispatch — the set that must be priced. Includes the codex
    fallback, since the seamless downgrade in run_one can route any codex run to it."""
    return {
        claude_model,
        codex_model,
        CODEX_FALLBACK_MODEL,
        SONNET_MODEL,
        *OPENROUTER_MODELS.values(),
    }



def require_priced(models):
    """Fail fast (before spending any tokens) if a model the run will dispatch has no price —
    an unpriced model silently mis-charges the daily budget. prices.json must stay in sync with
    the model registry (a CI test enforces it for the defaults)."""
    missing = sorted(m for m in models if m not in PRICES)
    if missing:
        raise SystemExit(f"unpriced model(s) {missing}: add them to prices.json "
                         f"(known: {sorted(PRICES)})")



def usage_totals(provider, usage):
    """Normalize provider usage to total input and its disjoint components.

    Claude and pi report fresh input separately from cache reads (and Claude cache writes).
    Codex reports total input with cached input as a subset. Keep the raw per-run usage intact;
    this view is for display and aggregate token accounting, not provider billing.
    """
    usage = usage if isinstance(usage, dict) else {}

    def count(key):
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    inp = count("input_tokens")
    cached = count("cache_read_input_tokens") if "cache_read_input_tokens" in usage else count("cached_input_tokens")
    written = count("cache_creation_input_tokens") if "cache_creation_input_tokens" in usage else count("cache_write_input_tokens")
    separate = (provider in {"claude", "sonnet"} or provider in OPENROUTER_MODELS
                or "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage)
    total = inp + cached + written if separate else inp
    fresh = inp if separate else max(0, inp - cached - written)
    return {"input_tokens": total, "fresh_input_tokens": fresh,
            "cached_input_tokens": cached, "cache_creation_input_tokens": written,
            "output_tokens": count("output_tokens"),
            "reasoning_output_tokens": count("reasoning_output_tokens")}


def sum_usage(run_results):
    """Comparable token totals across a round's runs, stored alongside its cost in the ledger."""
    t = {k: 0 for k in usage_totals(None, {})}
    for r in run_results:
        for k, value in usage_totals(r.get("provider"), r.get("usage")).items():
            t[k] += value
    return t



def fmt_tok(n):
    """Token counts for the visible footer: 299202 -> '299k', 3407 -> '3.4k', 950 -> '950'."""
    if not n:
        return "0"
    if n >= 1000:
        s = f"{n / 1000:.1f}"
        return (s[:-2] if s.endswith(".0") else s) + "k"
    return str(n)
