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
            if "cache_read" in w and not isinstance(w["cache_read"], (int, float)):
                bad.append(f"{model}@{w.get('effective')}.cache_read")
        # windows must be in chronological order so "newest" / "as-of-date" resolution is correct
        effs = [w.get("effective") for w in windows]
        if effs != sorted(effs):
            bad.append(f"{model}: windows not sorted by effective date {effs}")
    assert not bad, f"malformed price windows: {bad}"


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


def test_unpriced_model_inherits_the_newest_rate_on_its_line():
    """A Sol newer than the table bills at the newest Sol it knows, not at the generic default."""
    newest_sol = max((w["effective"], m) for m, w in pricing._PRICES_NOW.items()
                     if pricing.model_line(m) == pricing.model_line("gpt-6.1-sol"))[1]
    pin, pout, pcache, inherited = pricing.rates("gpt-7-sol")
    assert inherited == newest_sol
    assert (pin, pout) == review.PRICES[newest_sol]
    assert pcache == pricing.CACHE_READ[newest_sol]


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
