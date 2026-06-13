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


def test_price_entries_are_well_formed():
    bad = []
    for model, p in review._PRICES_RAW.items():
        for field in ("input", "output"):
            if not isinstance(p.get(field), (int, float)):
                bad.append(f"{model}.{field}")
        if "cache_read" in p and not isinstance(p["cache_read"], (int, float)):
            bad.append(f"{model}.cache_read")
    assert not bad, f"malformed price fields (want numbers): {bad}"


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
