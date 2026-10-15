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
                if not isinstance(lc.get("threshold"), int) or isinstance(lc.get("threshold"), bool):
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


def _write_fields(model):
    """The cache-write rate fields `model`'s provider bills by, and nothing else: Anthropic prices
    a write by its TTL, OpenAI prices one write rate, and the OpenRouter route reports no writes."""
    if "/" in model:
        return ()
    return ("cache_write_5m", "cache_write_1h") if model.startswith("claude-") else ("cache_write",)


def test_every_window_carries_exactly_the_write_rates_its_family_is_billed_at():
    """The worker vendors this table and prices prompt-cache writes from it, so a window missing
    its family's write rate is a KeyError there rather than a visible gap here. A field from the
    wrong family is the same error wearing the other provider's name, so both directions fail."""
    wrong = []
    for model, windows in review._PRICE_WINDOWS.items():
        wanted = set(_write_fields(model))
        for w in windows:
            for rates, where in ((w, model), (w.get("long_context") or {}, f"{model}.long_context")):
                if not rates:
                    continue
                present = {f for f in ("cache_write", "cache_write_5m", "cache_write_1h") if f in rates}
                wrong += [f"{where}@{w['effective']}: missing {f}" for f in wanted - present]
                wrong += [f"{where}@{w['effective']}: {f} is not billed on this route" for f in present - wanted]
    assert not wrong, f"windows whose write rates do not match their family: {wrong}"


def test_write_rates_are_their_providers_published_multiple_of_the_input_rate():
    """Both providers derive a cache write from the input rate: Anthropic bills 1.25x for a
    5-minute TTL and 2x for an hour, OpenAI 1.25x. Checking only that a write exceeds input
    accepts a 5x typo, and accepts the 5-minute and 1-hour rates swapped, which is the error two
    adjacent same-shaped numbers actually invite."""
    MULTIPLES = {"cache_write": 1.25, "cache_write_5m": 1.25, "cache_write_1h": 2.0}
    bad = []
    for model, windows in review._PRICE_WINDOWS.items():
        for w in windows:
            for rates, where in ((w, model), (w.get("long_context") or {}, f"{model}.long_context")):
                if not rates:
                    continue
                for field, multiple in MULTIPLES.items():
                    if field not in rates:
                        continue
                    want = round(rates["input"] * multiple, 10)
                    if round(rates[field], 10) != want:
                        bad.append(f"{where}@{w['effective']}: {field} {rates[field]}, expected {want}")
                if rates.get("cache_read", rates["input"]) > rates["input"]:
                    bad.append(f"{where}@{w['effective']}: cache_read {rates['cache_read']} exceeds input")
    assert not bad, f"cache rates off their provider's published multiplier: {bad}"


def test_long_context_tier_is_twice_input_and_half_again_output():
    """OpenAI states the >272K tier as 2x input and 1.5x output applied to the whole request, and
    discounts its cache reads from the tier's own input rate. Stating the tier as absolute numbers
    is clearer to read than a multiplier in code, but it does have to be the right numbers."""
    bad = []
    for model, windows in review._PRICE_WINDOWS.items():
        for w in windows:
            lc = w.get("long_context")
            if lc is None:
                continue
            for field, multiple in (("input", 2.0), ("output", 1.5), ("cache_read", 2.0)):
                if field not in lc:
                    continue
                want = round(w[field] * multiple, 10)
                if round(lc[field], 10) != want:
                    bad.append(f"{model}@{w['effective']}.long_context.{field} {lc[field]}, expected {want}")
            if lc.get("threshold") != 272000:
                bad.append(f"{model}@{w['effective']}.long_context.threshold {lc.get('threshold')}")
    assert not bad, f"long-context tiers off their published multipliers: {bad}"


def test_the_rates_in_force_are_the_providers_list_prices():
    """Literal, and deliberately tedious to update: every rate here was read off the provider's
    own page, and a dated table's failure mode is a number nobody notices is wrong. A genuine
    price change adds a window and updates one line here, with the provider page as the citation.
    `cache_read` is listed because it is not a fixed multiple of input: Opus 5.5 discounts reads
    to 0.05x where the rest of the Opus line charges 0.1x."""
    expected = {
        "claude-opus-4-8":          (5.0,   25.0, 0.5),
        "claude-opus-5":            (5.0,   25.0, 0.5),
        "claude-opus-5-5":          (4.0,   20.0, 0.2),
        "claude-sonnet-4-6":        (3.0,   15.0, 0.3),
        "claude-fable-5":           (10.0,  50.0, 1.0),
        "claude-fable-5-1":         (10.0,  50.0, 0.25),
        "claude-haiku-4-5":         (1.0,   5.0,  0.1),
        "gpt-5.5":                  (5.0,   30.0, 0.5),
        "gpt-5.6-sol":              (4.0,   20.0, 0.4),
        "gpt-5.6-terra":            (2.0,   12.0, 0.2),
        "gpt-5.6-luna":             (0.2,   1.2,  0.02),
        "gpt-6-sol":                (2.0,   10.0, 0.2),
        "gpt-6.1-sol":              (2.0,   10.0, 0.1),
        "gpt-6-luna":               (0.1,   0.5,  0.01),
        "deepseek/deepseek-v4-pro": (0.435, 0.87, 0.004),
        "minimax/minimax-m3":       (0.3,   1.2,  0.3),
        "x-ai/grok-4.3":            (1.25,  2.5,  1.25),
    }
    assert sorted(expected) == sorted(review._PRICE_WINDOWS), (
        "a model was added to or removed from prices.json without updating this list: "
        f"{sorted(set(expected) ^ set(review._PRICE_WINDOWS))}")
    for model, (pin, pout, pcache) in expected.items():
        assert pricing.rates(model)[:3] == (pin, pout, pcache), (
            f"{model} bills {pricing.rates(model)[:3]}, expected {(pin, pout, pcache)}")


def test_the_codex_fallback_is_priced_at_its_own_rates():
    """The seamless downgrade in run_one can route any codex rubric here, so the fallback's rates
    drive real spend and the daily budget as much as the default's do."""
    assert review.CODEX_FALLBACK_MODEL == "gpt-5.6-terra"
    assert review.PRICES[review.CODEX_FALLBACK_MODEL] == (2.0, 12.0)
    assert pricing.CACHE_READ[review.CODEX_FALLBACK_MODEL] == 0.2


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
