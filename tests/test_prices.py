#!/usr/bin/env python3
"""prices.json must stay in sync with the model registry.

Every model the engine can dispatch needs a price, or its review silently mis-charges the daily
budget (require_priced() now fails fast at runtime; this catches it at PR time, before merge).
Dependency-free — run with `python tests/test_prices.py` or under pytest.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
import review  # noqa: E402  (runner/ on path; same import the engine uses)


def test_every_dispatchable_model_is_priced():
    missing = sorted(m for m in review.dispatch_models() if m not in review.PRICES)
    assert not missing, (
        f"models the engine can dispatch but prices.json doesn't price: {missing}. "
        f"Add them to runner/prices.json (priced: {sorted(review.PRICES)})")


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


def test_require_priced_rejects_unknown_model():
    try:
        review.require_priced({"definitely-not-a-real-model"})
    except SystemExit:
        return
    raise AssertionError("require_priced() should SystemExit on an unpriced model")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nall {len(fns)} price checks passed "
          f"({len(review.dispatch_models())} dispatchable models, all priced)")
