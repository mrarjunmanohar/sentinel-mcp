"""Date boundary utilities for Shopify AI Sentinel.

Single source of truth for all period computations.
Weekly mode: last completed Mon-Sun vs prior Mon-Sun (7v7).
Rolling mode: Mon of current week through today (WTD) vs same days prior week (NvN).
Monthly mode: last completed calendar month vs prior month (MoM) or same month
              last year (YoY). See month_boundaries().
"""

from datetime import datetime, timedelta


def _last_day_of_month(dt: datetime) -> datetime:
    """Return the last day of the calendar month containing dt."""
    # Jump to day 28 (always valid), add 4 days to land in the next month,
    # snap to day 1, then step back one day → last day of dt's month.
    first_next = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_next - timedelta(days=1)


def week_boundaries(reference_date: str, mode: str = "weekly") -> tuple[str, str, str, str]:
    """Return (current_start, current_end, prior_start, prior_end).

    Both modes guarantee symmetric periods (same number of days).

    Weekly:
        Last completed Mon-Sun week vs the Mon-Sun before it.
        On Sunday, uses the PRIOR completed week (today's data may be
        incomplete due to Shopify's 24-48h analytics lag).

    Rolling:
        Monday of current week through reference_date (WTD)
        vs same weekdays of prior week. Always NvN days.
    """
    ref_dt = datetime.strptime(reference_date, "%Y-%m-%d")

    if mode == "weekly":
        # Find last completed Sunday.
        # On Sunday (weekday 6), use the PRIOR week — today's data is still settling.
        days_since_sunday = (ref_dt.weekday() + 1) % 7
        if days_since_sunday == 0:
            # Today is Sunday — go back to last Sunday (7 days ago)
            last_sunday = ref_dt - timedelta(days=7)
        else:
            last_sunday = ref_dt - timedelta(days=days_since_sunday)

        current_end = last_sunday.strftime("%Y-%m-%d")
        current_start = (last_sunday - timedelta(days=6)).strftime("%Y-%m-%d")
        prior_end = (last_sunday - timedelta(days=7)).strftime("%Y-%m-%d")
        prior_start = (last_sunday - timedelta(days=13)).strftime("%Y-%m-%d")

    elif mode == "rolling":
        # Monday of current ISO week through today
        current_start_dt = ref_dt - timedelta(days=ref_dt.weekday())  # Monday
        current_end_dt = ref_dt

        # Same days of prior week
        prior_start_dt = current_start_dt - timedelta(days=7)
        prior_end_dt = current_end_dt - timedelta(days=7)

        current_start = current_start_dt.strftime("%Y-%m-%d")
        current_end = current_end_dt.strftime("%Y-%m-%d")
        prior_start = prior_start_dt.strftime("%Y-%m-%d")
        prior_end = prior_end_dt.strftime("%Y-%m-%d")

    else:
        raise ValueError(f"Unknown mode: {mode!r}. Use 'weekly' or 'rolling'.")

    return current_start, current_end, prior_start, prior_end


def month_boundaries(reference_date: str, compare: str = "mom") -> tuple[str, str, str, str]:
    """Return (current_start, current_end, prior_start, prior_end) for monthly mode.

    Current period is always the LAST COMPLETED calendar month (the month before
    reference_date's month) — never a partial or rolling window.

    compare:
        "mom" — prior = the calendar month immediately before current
                (e.g. May 2026 vs Apr 2026). Periods may differ in length
                (28-31 days); the displayed PoP delta uses raw totals.
        "yoy" — prior = the same calendar month one year earlier
                (e.g. May 2026 vs May 2025). Requires prior-year data to be
                already backfilled in the DB.
    """
    ref_dt = datetime.strptime(reference_date, "%Y-%m-%d")

    # Last completed month = month before reference_date's month.
    current_end_dt = ref_dt.replace(day=1) - timedelta(days=1)  # last day of prior month
    current_start_dt = current_end_dt.replace(day=1)

    if compare == "mom":
        prior_end_dt = current_start_dt - timedelta(days=1)
        prior_start_dt = prior_end_dt.replace(day=1)
    elif compare == "yoy":
        # day=1 is always valid, so year-1 is safe even for Feb 29.
        prior_start_dt = current_start_dt.replace(year=current_start_dt.year - 1)
        prior_end_dt = _last_day_of_month(prior_start_dt)
    else:
        raise ValueError(f"Unknown compare: {compare!r}. Use 'mom' or 'yoy'.")

    return (
        current_start_dt.strftime("%Y-%m-%d"),
        current_end_dt.strftime("%Y-%m-%d"),
        prior_start_dt.strftime("%Y-%m-%d"),
        prior_end_dt.strftime("%Y-%m-%d"),
    )


def period_boundaries(
    reference_date: str, report_mode: str = "weekly", compare: str = "mom"
) -> tuple[str, str, str, str]:
    """Dispatch to the right boundary function for a report mode.

    Single entry point so callers don't branch on mode. Weekly/rolling go to
    week_boundaries(); monthly goes to month_boundaries() with the MoM/YoY switch.
    """
    if report_mode == "monthly":
        return month_boundaries(reference_date, compare)
    return week_boundaries(reference_date, report_mode)


def fetch_since_days(reference_date: str, mode: str = "weekly", compare: str = "mom") -> int:
    """Minimum days to fetch from Shopify to cover the reporting windows + lag buffer.

    The z-score lookback uses pre-backfilled data already in the DB; this only
    covers what must be freshly fetched each run.

    Monthly mode caps the live fetch at ~65 days — enough for the current month
    plus a prior month (MoM). YoY does NOT widen the live fetch: the prior-year
    month is expected to already be in the DB (run fetch-history), and
    compute_pop_change reads it from there.
    """
    ref_dt = datetime.strptime(reference_date, "%Y-%m-%d")

    if mode == "monthly":
        # Cover current month + immediately prior month + 2-day lag buffer,
        # regardless of compare (YoY prior year comes from the DB, not live fetch).
        _, _, prior_start, _ = month_boundaries(reference_date, "mom")
        prior_start_dt = datetime.strptime(prior_start, "%Y-%m-%d")
        days_needed = (ref_dt - prior_start_dt).days + 2
        return max(days_needed, 35)

    _, _, prior_start, _ = week_boundaries(reference_date, mode)
    prior_start_dt = datetime.strptime(prior_start, "%Y-%m-%d")

    # Days from prior_start to today + 2-day buffer for Shopify analytics lag
    days_needed = (ref_dt - prior_start_dt).days + 2
    return max(days_needed, 21)  # floor at 21 days


def days_in_period(boundaries: tuple[str, str, str, str]) -> int:
    """Number of days in the current period (inclusive)."""
    start = datetime.strptime(boundaries[0], "%Y-%m-%d")
    end = datetime.strptime(boundaries[1], "%Y-%m-%d")
    return (end - start).days + 1
