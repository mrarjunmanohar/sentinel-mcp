"""Tests for conversion_rate scoring in analyze_kpis.

Covers two correctness rules (see CLAUDE.md):
  1. conversion_rate is ALWAYS period-total sum(orders)/sum(sessions),
     never the average of daily rates (Simpson's paradox).
  2. Low-volume suppression keys off the REPORTING PERIOD's session volume,
     not a single unrelated day (the reference date).

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_conversion_scoring
"""

import os
import sqlite3
import tempfile

from sentinel.db import init_db, insert_kpi_snapshots_bulk
from sentinel.anomaly import analyze_kpis


def _fresh_db() -> sqlite3.Connection:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert(conn, rows: list[tuple[str, str, float]]):
    insert_kpi_snapshots_bulk(conn, rows)
    conn.commit()


def _conversion(metrics):
    return next(m for m in metrics if m.name == "conversion_rate")


def _week(start_day: int, orders_per_day: float, sessions_per_day: float, month: str = "05"):
    """7 daily (orders, sessions) rows for a Mon-Sun week starting 2026-<month>-<start_day>."""
    rows = []
    for i in range(7):
        d = f"2026-{month}-{start_day + i:02d}"
        rows.append((d, "orders", orders_per_day))
        rows.append((d, "sessions", sessions_per_day))
    return rows


def test_weekly_conversion_uses_period_total_not_daily_average():
    # Current week (2026-06-01..06-07) has two wildly asymmetric days:
    #   06-01: 1 order / 10 sessions  (10%)
    #   06-02: 0 orders / 200 sessions (0%)
    # avg-of-daily = 5%   ;   period-total = 1/210 = 0.4762%
    conn = _fresh_db()
    rows = [
        ("2026-06-01", "orders", 1.0), ("2026-06-01", "sessions", 10.0),
        ("2026-06-02", "orders", 0.0), ("2026-06-02", "sessions", 200.0),
    ]
    # Two prior weeks so the metric is produced (baseline non-empty).
    rows += _week(18, 10.0, 1000.0)  # 2026-05-18..05-24
    rows += _week(25, 10.0, 1000.0)  # 2026-05-25..05-31
    _insert(conn, rows)

    metrics = analyze_kpis(conn, "2026-06-10", report_mode="weekly")
    cr = _conversion(metrics)

    expected = 1.0 / 210.0
    assert abs(cr.current_value - expected) < 1e-9, (
        f"conversion current_value={cr.current_value:.6f}, "
        f"expected period-total {expected:.6f} (not avg-of-daily 0.05)"
    )


def test_monthly_conversion_uses_period_total_not_daily_average():
    # Current month May has the same asymmetric shape → period-total, not avg.
    conn = _fresh_db()
    rows = [
        ("2026-05-01", "orders", 1.0), ("2026-05-01", "sessions", 10.0),
        ("2026-05-02", "orders", 0.0), ("2026-05-02", "sessions", 200.0),
    ]
    # A prior month with >=20 days so aggregate_monthly yields a baseline window.
    for day in range(1, 26):
        d = f"2026-04-{day:02d}"
        rows.append((d, "orders", 10.0))
        rows.append((d, "sessions", 1000.0))
    _insert(conn, rows)

    metrics = analyze_kpis(conn, "2026-06-10", report_mode="monthly")
    cr = _conversion(metrics)

    expected = 1.0 / 210.0
    assert abs(cr.current_value - expected) < 1e-9, (
        f"monthly conversion current_value={cr.current_value:.6f}, "
        f"expected period-total {expected:.6f}"
    )


def test_conversion_not_suppressed_by_low_reference_day():
    # The reporting week (2026-06-01..06-07) has thousands of sessions and a
    # 10% conversion anomaly vs a ~1% baseline. The reference date 2026-06-10
    # (outside the period) has only 2 sessions. Suppression must NOT fire on
    # that single unrelated day, so the anomaly's z-score survives.
    conn = _fresh_db()
    rows = []
    # Four baseline weeks with low, slightly varying CR (~0.8-1.2%).
    rows += _week(4, 8.0, 1000.0)    # 0.8%
    rows += _week(11, 10.0, 1000.0)  # 1.0%
    rows += _week(18, 12.0, 1000.0)  # 1.2%
    rows += _week(25, 9.0, 1000.0)   # 0.9%
    # Current week: high volume, 10% CR anomaly.
    rows += _week(1, 100.0, 1000.0, month="06")  # 2026-06-01..06-07
    # Reference date itself: near-zero traffic.
    rows += [("2026-06-10", "orders", 0.0), ("2026-06-10", "sessions", 2.0)]
    _insert(conn, rows)

    metrics = analyze_kpis(conn, "2026-06-10", report_mode="weekly")
    cr = _conversion(metrics)

    assert abs(cr.z_score) > 0.5, (
        f"conversion z_score={cr.z_score:.3f} — anomaly was suppressed by the "
        f"low-traffic reference day instead of using period volume"
    )


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run() else 0)
