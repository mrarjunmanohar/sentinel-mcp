"""Tests for assemble_sales_channel_breakdown (the 'Sales Channel Breakdown' table).

Pure function — no I/O. Run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_sales_channel_breakdown
"""

from sentinel.report_builder import assemble_sales_channel_breakdown


def _row(channel, orders, items, gross):
    return {"sales_channel": channel, "orders": orders,
            "net_items_sold": items, "gross_sales": gross}


def _current_7():
    return [
        _row("Online Store", 60, 142, 132400),
        _row("Hello December", 6, 11, 6900),
        _row("POS", 5, 9, 5200),
        _row("Shop app", 3, 5, 2300),
        _row("Google", 2, 3, 1500),
        _row("Instagram", 1, 2, 900),
        _row("Draft Orders", 1, 1, 400),
    ]


def test_top5_plus_total_row():
    rows = assemble_sales_channel_breakdown(_current_7(), [], top_n=5)
    channel_rows = [r for r in rows if not r.is_total]
    totals = [r for r in rows if r.is_total]
    assert len(channel_rows) == 5, f"expected 5 channel rows, got {len(channel_rows)}"
    assert len(totals) == 1, "expected exactly one TOTAL row"
    # Sorted by gross_sales desc → Online Store first.
    assert channel_rows[0].channel == "Online Store"


def test_total_sums_all_channels_not_just_top5():
    rows = assemble_sales_channel_breakdown(_current_7(), [], top_n=5)
    total = next(r for r in rows if r.is_total)
    assert total.orders == 78          # 60+6+5+3+2+1+1
    assert total.net_items_sold == 173  # 142+11+9+5+3+2+1
    assert total.gross_sales == 149600  # sum of all 7, not just top 5


def test_gross_sales_change_vs_prior():
    current = [_row("Online Store", 60, 142, 120000)]
    prior = [_row("Online Store", 50, 130, 100000)]
    rows = assemble_sales_channel_breakdown(current, prior, top_n=5)
    online = next(r for r in rows if r.channel == "Online Store")
    assert abs(online.prior_gross_sales - 100000) < 1e-9
    assert abs(online.gross_sales_change - 0.20) < 1e-9  # +20%


def test_new_channel_has_no_change():
    current = [_row("Hello December", 6, 11, 6900)]
    prior = []  # channel absent last period
    rows = assemble_sales_channel_breakdown(current, prior, top_n=5)
    hd = next(r for r in rows if r.channel == "Hello December")
    assert hd.prior_gross_sales is None
    assert hd.gross_sales_change is None


def test_total_change_vs_prior_total():
    current = [_row("A", 1, 1, 80), _row("B", 1, 1, 20)]   # total 100
    prior = [_row("A", 1, 1, 100), _row("B", 1, 1, 100)]   # total 200
    rows = assemble_sales_channel_breakdown(current, prior, top_n=5)
    total = next(r for r in rows if r.is_total)
    assert abs(total.gross_sales_change - (-0.5)) < 1e-9   # 100 vs 200 = -50%


def test_empty_current_returns_empty():
    assert assemble_sales_channel_breakdown([], [], top_n=5) == []


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
