#!/usr/bin/env python3
"""prices.json must stay in sync with the model registry.

Every model the engine can dispatch needs a price, or its review mis-charges the daily budget.
An absent row no longer stops the run (warn_unpriced() inherits the newest rate on the model's
line), so this is the check that keeps the defaults billed exactly rather than approximately.
Dependency-free — run with `python tests/test_prices.py` or under pytest.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import review  # noqa: E402  (runner/ on path; same import the engine uses)
import pricing  # noqa: E402


def test_every_dispatchable_model_is_priced():
    missing = sorted(m for m in review.dispatch_models() if m not in review.PRICES)
    assert not missing, (
        f"models the engine can dispatch but prices.json doesn't price: {missing}. "
        f"Add them to runner/prices.json (priced: {sorted(review.PRICES)})")


def test_default_claude_is_opus_55_with_its_own_rates():
    assert review.CLAUDE_MODEL == "claude-opus-5-5"
    assert review.PRICES[review.CLAUDE_MODEL] == (4.0, 20.0)
    assert pricing.CACHE_READ[review.CLAUDE_MODEL] == 0.2


def test_default_codex_is_sol_61_with_its_own_rates():
    assert review.CODEX_MODEL == "gpt-6.1-sol"
    assert review.PRICES[review.CODEX_MODEL] == (2.0, 10.0)
    assert pricing.CACHE_READ[review.CODEX_MODEL] == 0.1


def test_price_windows_are_well_formed():
    bad = []
    for model, windows in review._PRICE_WINDOWS.items():
        if not windows:
            bad.append(f"{model}: no rate windows")
            continue
        for w in windows:
            if not isinstance(w.get("effective"), str):
                bad.append(f"{model}: window missing `effective` date")
            for field in ("input", "output"):
                if not isinstance(w.get(field), (int, float)):
                    bad.append(f"{model}@{w.get('effective')}.{field}")
            for field in ("cache_read", "cache_write", "cache_write_5m", "cache_write_1h"):
                if field in w and not isinstance(w[field], (int, float)):
                    bad.append(f"{model}@{w.get('effective')}.{field}")
            lc = w.get("long_context")
            if lc is not None:
                if not isinstance(lc.get("threshold"), int):
                    bad.append(f"{model}@{w.get('effective')}.long_context.threshold")
                for field in ("input", "output"):
                    if not isinstance(lc.get(field), (int, float)):
                        bad.append(f"{model}@{w.get('effective')}.long_context.{field}")
                for field in ("cache_read", "cache_write"):
                    if field in lc and not isinstance(lc[field], (int, float)):
                        bad.append(f"{model}@{w.get('effective')}.long_context.{field}")
        # windows must be in chronological order so "newest" / "as-of-date" resolution is correct
        effs = [w.get("effective") for w in windows]
        if effs != sorted(effs):
            bad.append(f"{model}: windows not sorted by effective date {effs}")
    assert not bad, f"malformed price windows: {bad}"


def test_every_window_carries_the_write_rates_its_family_is_billed_at():
    """The worker vendors this table and prices prompt-cache writes from it, so a window missing
    its family's write rate is a KeyError there rather than a visible gap here. Anthropic bills
    writes by TTL (two rates), OpenAI bills one, and the OpenRouter route reports no writes at
    all."""
    missing = []
    for model, windows in review._PRICE_WINDOWS.items():
        if "/" in model:  # OpenRouter-routed: cache reads only
            continue
        needed = ("cache_write_5m", "cache_write_1h") if model.startswith("claude-") else ("cache_write",)
        for w in windows:
            missing += [f"{model}@{w['effective']}.{f}" for f in needed if f not in w]
            lc = w.get("long_context")
            if lc is not None and "cache_write" not in lc:
                missing.append(f"{model}@{w['effective']}.long_context.cache_write")
    assert not missing, f"windows missing a write rate their family is billed at: {missing}"


def test_cache_rates_bracket_the_input_rate():
    """A cache read is cheaper than fresh input and a cache write dearer, for every provider here.
    A rate that falls outside that bracket is a transcription error, not a tariff."""
    bad = []
    for model, windows in review._PRICE_WINDOWS.items():
        for w in windows:
            for rates, where in ((w, model), (w.get("long_context") or {}, f"{model}.long_context")):
                pin = rates.get("input")
                if pin is None:
                    continue
                if rates.get("cache_read", pin) > pin:
                    bad.append(f"{where}@{w['effective']}: cache_read {rates['cache_read']} > input {pin}")
                for field in ("cache_write", "cache_write_5m", "cache_write_1h"):
                    if field in rates and rates[field] < pin:
                        bad.append(f"{where}@{w['effective']}: {field} {rates[field]} < input {pin}")
    assert not bad, f"cache rates outside the read <= input <= write bracket: {bad}"


def test_warn_unpriced_does_not_stop_the_run():
    """An out-of-date table must not be an outage: it is what kept the codex default pinned to a
    superseded model, because the model could not be bumped before its prices landed."""
    pricing.warn_unpriced({"definitely-not-a-real-model"})


def test_model_line_groups_successive_releases():
    for a, b in (("gpt-6.1-sol", "gpt-5.6-sol"),
                 ("claude-opus-5-6", "claude-opus-5-5"),
                 ("x-ai/grok-5", "x-ai/grok-4.3")):
        assert pricing.model_line(a) == pricing.model_line(b), f"{a} and {b} should share a line"
    for a, b in (("gpt-6.1-sol", "gpt-6-luna"),
                 ("gpt-5.6-sol", "gpt-5.6-terra"),
                 ("claude-opus-5-5", "claude-sonnet-4-6")):
        assert pricing.model_line(a) != pricing.model_line(b), f"{a} and {b} are different lines"


def test_unpriced_model_inherits_from_the_newest_model_on_its_line():
    """Literal expectations on purpose. Deriving them with the implementation's own ranking rule
    would only assert that the code agrees with itself, which passes for a wrong rule too. Update
    these when a newer Sol or Opus is priced."""
    assert pricing.rates("gpt-99-sol")[3] == "gpt-6.1-sol"
    assert pricing.rates("gpt-99-sol")[:3] == (2.0, 10.0, 0.1)
    assert pricing.rates("claude-opus-9-9")[3] == "claude-opus-5-5"
    assert pricing.rates("claude-opus-9-9")[:3] == (4.0, 20.0, 0.2)


def test_newest_sibling_prefers_the_newest_release_not_the_newest_reprice():
    """A legacy-tier discount on a retired model must not capture its line. Ranking by date alone
    bills a newer Sol at the retired model's rate, and under-billing is the dangerous direction:
    the daily budget guard is driven off this number."""
    windows = {"gpt-5.6-sol": {"effective": "2026-11-01", "input": 1.0, "output": 6.0},
               "gpt-6.1-sol": {"effective": "2026-09-29", "input": 2.0, "output": 10.0}}
    source, window = pricing.newest_sibling("gpt-7-sol", windows)
    assert source == "gpt-6.1-sol", f"inherited from the repriced retired model: {source}"
    assert window["input"] == 2.0


def test_newest_sibling_orders_versions_numerically_not_as_strings():
    windows = {"gpt-9-sol": {"effective": "2026-12-01", "input": 1.0, "output": 5.0},
               "gpt-10-sol": {"effective": "2026-12-01", "input": 20.0, "output": 100.0}}
    source, _ = pricing.newest_sibling("gpt-11-sol", windows)
    assert source == "gpt-10-sol", f"ASCII order picks gpt-9-sol; got {source}"


def test_current_window_ignores_windows_not_yet_in_effect():
    """A rate change can be landed ahead of its date the way providers announce it, so a future
    window must not bill early."""
    live = {"effective": "2026-01-01", "input": 2.0, "output": 10.0}
    future = {"effective": "2099-01-01", "input": 99.0, "output": 99.0}
    assert pricing._current_window([live, future])["input"] == 2.0
    # ... but a model priced only from a future date still has to yield some price
    assert pricing._current_window([future])["input"] == 99.0


def test_model_line_treats_a_differently_suffixed_tier_as_its_own_line():
    """A tier suffix is not a version, so a `-mini` variant does not inherit the full tier's rates
    and falls to DEFAULT_PRICE. That over-bills rather than under-bills, which is the safe
    direction, but it is a real limit of grouping by naming convention."""
    assert pricing.model_line("gpt-6.1-sol-mini") != pricing.model_line("gpt-6.1-sol")
    assert pricing.rates("gpt-6.1-sol-mini")[3] == pricing.DEFAULT_PRICE_SOURCE


def test_rates_is_total():
    """`PRICES.get(model, DEFAULT_PRICE)` could not raise, and rates() replaced it on every
    billing path, so it must not become the thing that throws."""
    for bad in (None, 5, 3.5, True, b"x", (), [], {}):
        pin, pout, pcache, inherited = pricing.rates(bad)
        assert (pin, pout) == pricing.DEFAULT_PRICE, f"rates({bad!r})"
        assert inherited == pricing.DEFAULT_PRICE_SOURCE
    pricing.warn_unpriced({None, "gpt-99-sol"})


def test_unpriced_model_with_no_priced_sibling_falls_back_to_the_default():
    pin, pout, pcache, inherited = pricing.rates("nothing-like-this-exists")
    assert inherited == pricing.DEFAULT_PRICE_SOURCE
    assert (pin, pout) == pricing.DEFAULT_PRICE
    assert pcache == pricing.DEFAULT_PRICE[0]


def test_priced_model_reports_no_inheritance():
    assert pricing.rates(review.CODEX_MODEL)[3] is None
    assert pricing.rates(review.CLAUDE_MODEL)[3] is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nall {len(fns)} price checks passed "
          f"({len(review.dispatch_models())} dispatchable models, all priced)")
