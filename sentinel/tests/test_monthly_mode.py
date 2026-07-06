"""Tests for monthly reporting mode boundaries and aggregation.

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_monthly_mode
"""

from sentinel.date_utils import month_boundaries, period_boundaries, fetch_since_days
from sentinel.anomaly import aggregate_monthly


def test_mom_mid_month():
    # Run mid-month → last completed month is the prior month.
    assert month_boundaries("2026-06-04", "mom") == (
        "2026-05-01", "2026-05-31", "2026-04-01", "2026-04-30"
    )


def test_mom_run_on_first():
    # Running on the 1st still uses the last completed month (May), not June.
    assert month_boundaries("2026-06-01", "mom") == (
        "2026-05-01", "2026-05-31", "2026-04-01", "2026-04-30"
    )


def test_mom_year_rollover():
    # January run → current Dec of prior year, prior Nov.
    assert month_boundaries("2026-01-15", "mom") == (
        "2025-12-01", "2025-12-31", "2025-11-01", "2025-11-30"
    )


def test_yoy_basic():
    assert month_boundaries("2026-06-04", "yoy") == (
        "2026-05-01", "2026-05-31", "2025-05-01", "2025-05-31"
    )


def test_yoy_leap_year_feb():
    # Current Feb 2025 (28 days) compared to Feb 2024 (29 days, leap).
    # The year-1 replace must not crash and must yield the correct month-end.
    assert month_boundaries("2025-03-10", "yoy") == (
        "2025-02-01", "2025-02-28", "2024-02-01", "2024-02-29"
    )


def test_period_boundaries_dispatch():
    # monthly routes to month_boundaries; weekly/rolling fall through unchanged.
    assert period_boundaries("2026-06-04", "monthly", "mom") == month_boundaries("2026-06-04", "mom")
    wk = period_boundaries("2026-06-04", "weekly")
    assert len(wk) == 4 and wk[0] <= wk[1]


def test_fetch_since_caps_monthly():
    # Monthly fetch covers current + prior month (~66 days), and YoY does NOT
    # widen it (prior-year data is expected pre-backfilled in the DB).
    mom = fetch_since_days("2026-06-04", "monthly", "mom")
    yoy = fetch_since_days("2026-06-04", "monthly", "yoy")
    assert mom == yoy
    assert 35 <= mom <= 75


def test_aggregate_monthly_daily_normalized():
    # 31 days of value=10 → daily-average window = 10.0 (NOT the raw sum 310).
    # This is what keeps the z-score baseline length-invariant.
    daily = [(f"2026-01-{d:02d}", 10.0) for d in range(1, 32)]
    agg = aggregate_monthly(daily, use_average=False)
    assert agg == [("2026-01", 10.0)]


def test_aggregate_monthly_partial_month_guard():
    # A month with < 20 days of data is excluded entirely.
    daily = [(f"2026-02-{d:02d}", 5.0) for d in range(1, 10)]  # only 9 days
    assert aggregate_monthly(daily) == []


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run() else 0)
