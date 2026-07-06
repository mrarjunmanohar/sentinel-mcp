"""Z-score anomaly detection engine for Shopify AI Sentinel."""

import math
from datetime import datetime, timedelta

from .models import AlertLevel, KPIMetric, FunnelStep
from .db import query_kpi_range

# KPIs to analyze
CORE_KPIS = [
    "total_sales",
    "sessions",
    "conversion_rate",
    "average_order_value",
    "net_sales",
    "orders",
]

# Extended KPIs for funnel analysis
FUNNEL_KPIS = [
    "sessions_with_cart_additions",
    "sessions_that_reached_checkout",
    "sessions_that_completed_checkout",
]

# Sensitivity multipliers for alert thresholds
SENSITIVITY = {
    "LOW": {"warning": 2.0, "critical": 3.0},
    "MED": {"warning": 1.5, "critical": 2.5},
    "HIGH": {"warning": 1.0, "critical": 2.0},
}


def compute_rolling_stats(values: list[float], window: int = 30) -> tuple[float, float]:
    """Compute mean and standard deviation over a rolling window.

    Uses the last `window` values. Returns (mean, stddev).
    If fewer than 3 values, stddev is 0 to avoid noisy signals.
    """
    if not values:
        return 0.0, 0.0

    windowed = values[-window:]
    n = len(windowed)

    if n < 3:
        return sum(windowed) / n, 0.0

    mean = sum(windowed) / n
    variance = sum((x - mean) ** 2 for x in windowed) / (n - 1)  # sample variance
    stddev = math.sqrt(variance)

    return mean, stddev


def compute_z_score(current: float, mean: float, stddev: float) -> float:
    """Compute z-score. Returns 0 if stddev is 0."""
    if stddev == 0:
        return 0.0
    return (current - mean) / stddev


def _group_by_week(daily_data: list[tuple[str, float]]) -> dict[str, list[float]]:
    """Group daily data by ISO week (Monday start)."""
    weeks: dict[str, list[float]] = {}
    for date_str, value in daily_data:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        week_start = dt - timedelta(days=dt.weekday())
        week_key = week_start.strftime("%Y-%m-%d")
        if week_key not in weeks:
            weeks[week_key] = []
        weeks[week_key].append(value)
    return weeks


def aggregate_weekly(daily_data: list[tuple[str, float]], use_average: bool = False) -> list[tuple[str, float]]:
    """Aggregate daily data into 7-day windows.

    Args:
        daily_data: List of (date, value) tuples.
        use_average: If True, average values (for rates). If False, sum (for counts).
    """
    if not daily_data:
        return []

    weeks = _group_by_week(daily_data)
    result = []
    for week_key in sorted(weeks.keys()):
        values = weeks[week_key]
        if len(values) >= 5:  # Only include weeks with enough data
            agg = (sum(values) / len(values)) if use_average else sum(values)
            result.append((week_key, agg))

    return result


def _group_by_month(daily_data: list[tuple[str, float]]) -> dict[str, list[float]]:
    """Group daily data by calendar month (YYYY-MM)."""
    months: dict[str, list[float]] = {}
    for date_str, value in daily_data:
        month_key = date_str[:7]  # "YYYY-MM"
        months.setdefault(month_key, []).append(value)
    return months


def aggregate_monthly(daily_data: list[tuple[str, float]], use_average: bool = False) -> list[tuple[str, float]]:
    """Aggregate daily data into calendar-month windows.

    For count metrics (use_average=False) the window value is the average per
    day (sum / days present), NOT the raw monthly sum. This keeps the z-score
    baseline length-invariant: a 31-day month and a 28-day month are compared on
    equal footing. The report's PoP table still displays raw monthly totals —
    only the anomaly scoring is daily-normalized.

    For rate metrics (use_average=True) the window value is the simple average,
    which is already length-invariant.

    Months with fewer than 20 days of data are excluded (partial-month guard,
    analogous to the >=5/week guard in aggregate_weekly).
    """
    if not daily_data:
        return []

    months = _group_by_month(daily_data)
    result = []
    for month_key in sorted(months.keys()):
        values = months[month_key]
        if len(values) >= 20:  # Only include months with enough data
            # Both counts and rates use the daily average here: rates are
            # naturally per-day, and counts are normalized to per-day to remove
            # month-length variance from the baseline.
            agg = sum(values) / len(values)
            result.append((month_key, agg))

    return result


def _aggregate_ratio(
    num_data: list[tuple[str, float]],
    den_data: list[tuple[str, float]],
    report_mode: str,
    min_days: int,
) -> list[tuple[str, float]]:
    """Aggregate two daily series into period windows as sum(num)/sum(den).

    Used for conversion_rate so each window is the period-total ratio
    (e.g. sum(orders)/sum(sessions) per week/month), NOT the average of daily
    ratios — avoids Simpson's paradox in the baseline (see CLAUDE.md).

    Windows with fewer than `min_days` of denominator data, or zero total
    denominator, are dropped (same partial-period guard as the aggregators).
    """
    group = _group_by_month if report_mode == "monthly" else _group_by_week
    num_groups = group(num_data)
    den_groups = group(den_data)
    result = []
    for key in sorted(den_groups):
        den_vals = den_groups[key]
        den_sum = sum(den_vals)
        if len(den_vals) >= min_days and den_sum > 0:
            num_sum = sum(num_groups.get(key, []))
            result.append((key, num_sum / den_sum))
    return result


def classify_alert(z_score: float, sensitivity: str = "MED") -> AlertLevel:
    """Classify alert level based on z-score and sensitivity setting."""
    thresholds = SENSITIVITY.get(sensitivity, SENSITIVITY["MED"])
    score = abs(z_score)
    if score > thresholds["critical"]:
        return AlertLevel.CRITICAL
    elif score > thresholds["warning"]:
        return AlertLevel.WARNING
    return AlertLevel.NORMAL


def compute_overall_status(
    metrics: list[KPIMetric],
    pop_changes: dict[str, float],
    pop_warning_threshold: float = -0.30,
) -> AlertLevel:
    """Programmatic overall status. Code decides, LLM explains.

    Rules:
    - ANY z-score CRITICAL → CRITICAL
    - ANY z-score WARNING, OR any core KPI PoP drop exceeds threshold → WARNING
    - Otherwise → NORMAL
    """
    for m in metrics:
        if m.status == AlertLevel.CRITICAL:
            return AlertLevel.CRITICAL
    for m in metrics:
        if m.status == AlertLevel.WARNING:
            return AlertLevel.WARNING
    for kpi_name, pct_change in pop_changes.items():
        if kpi_name in CORE_KPIS and pct_change <= pop_warning_threshold:
            return AlertLevel.WARNING
    return AlertLevel.NORMAL


def should_suppress(metric_name: str, sessions_count: float | None, threshold: int = 50) -> bool:
    """Check if an alert should be suppressed due to low volume.

    Conversion rate alerts are suppressed when daily sessions < threshold.
    """
    if metric_name in ("conversion_rate",) and sessions_count is not None:
        return sessions_count < threshold
    return False


def normalize_by_day_of_week(
    data: list[tuple[str, float]], target_date: str
) -> list[tuple[str, float]]:
    """Filter data to only include same day-of-week as target date.

    Useful for comparing Monday to prior Mondays, etc.
    """
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    target_weekday = target_dt.weekday()

    return [
        (date_str, value)
        for date_str, value in data
        if datetime.strptime(date_str, "%Y-%m-%d").weekday() == target_weekday
    ]


def _get_sessions_for_date(conn, date_str: str) -> float | None:
    """Helper to get session count for a specific date."""
    results = query_kpi_range(conn, "sessions", date_str, date_str)
    if results:
        return results[0][1]
    return None


def analyze_kpis(
    conn,
    target_date: str,
    weekly_mode: bool = False,
    report_mode: str = "daily",
    sensitivity: str = "MED",
    day_of_week_normalize: bool = True,
) -> list[KPIMetric]:
    """Main entry point: analyze all KPIs for a target date.

    Args:
        conn: SQLite connection
        target_date: Date to analyze (YYYY-MM-DD)
        weekly_mode: If True, use weekly aggregation for smoothing
        report_mode: "daily", "weekly", or "rolling" — controls current-value window
        sensitivity: Alert sensitivity (LOW/MED/HIGH)
        day_of_week_normalize: If True, compare to same weekday only

    Returns:
        List of KPIMetric objects with computed z-scores
    """
    from .date_utils import period_boundaries

    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    # Monthly mode aggregates the baseline into calendar-month windows, so a
    # 365-day lookback yields only ~12 points. Widen to 730 days (~24 months)
    # for a more stable mean/stddev. Requires fetch-history backfill to fill it.
    is_monthly = report_mode == "monthly"
    lookback_days = 730 if is_monthly else 365
    lookback_start = (target_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # Resolve current period boundaries for period-based modes
    use_period_mode = report_mode in ("weekly", "rolling", "monthly")
    if use_period_mode:
        # compare basis is irrelevant for the CURRENT period (it only affects
        # the prior period, which analyze_kpis does not use here).
        period_start, period_end, _, _ = period_boundaries(target_date, report_mode)

    # Sessions volume for the low-volume suppression check. In period modes this
    # is the TOTAL sessions over the reporting window — not a single day. Keying
    # off the reference date (which falls OUTSIDE the period for weekly/monthly)
    # would suppress real anomalies whenever the current partial day is quiet.
    if use_period_mode:
        period_sessions = query_kpi_range(conn, "sessions", period_start, period_end)
        sessions_count = sum(v for _, v in period_sessions) if period_sessions else 0.0
    else:
        sessions_count = _get_sessions_for_date(conn, target_date)

    metrics = []
    rate_metrics = {"conversion_rate", "average_order_value"}  # Metrics that are rates/averages, not counts

    for kpi_name in CORE_KPIS:
        # Get historical data
        if kpi_name == "conversion_rate":
            # Compute from orders/sessions instead of Shopify's daily rate,
            # which averages to near-zero on low-traffic days and misleads z-scores.
            orders_data = dict(query_kpi_range(conn, "orders", lookback_start, target_date))
            sessions_data = dict(query_kpi_range(conn, "sessions", lookback_start, target_date))
            all_dates = sorted(set(orders_data) | set(sessions_data))
            raw_data = []
            for d in all_dates:
                s = sessions_data.get(d, 0)
                o = orders_data.get(d, 0)
                if s > 0:
                    raw_data.append((d, o / s))
        else:
            raw_data = query_kpi_range(conn, kpi_name, lookback_start, target_date)

        if not raw_data:
            continue

        # Exclude current date from baseline
        baseline_data = [(d, v) for d, v in raw_data if d < target_date]

        if not baseline_data:
            continue

        # days_in_current is set for monthly count metrics so the z-score can be
        # computed on a length-invariant per-day basis (see below).
        days_in_current = 0
        if use_period_mode and kpi_name == "conversion_rate":
            # Conversion is ALWAYS period-total sum(orders)/sum(sessions), for
            # both the current value AND each baseline window — never the average
            # of daily rates (Simpson's paradox; see CLAUDE.md).
            cur_orders = sum(o for d, o in orders_data.items() if period_start <= d <= period_end)
            cur_sessions = sum(s for d, s in sessions_data.items() if period_start <= d <= period_end)
            current_value = (cur_orders / cur_sessions) if cur_sessions > 0 else 0.0

            min_days = 20 if is_monthly else 5  # match aggregate_monthly / aggregate_weekly guards
            base_orders = [(d, o) for d, o in orders_data.items() if d < target_date]
            base_sessions = [(d, s) for d, s in sessions_data.items() if d < target_date]
            baseline_data = _aggregate_ratio(base_orders, base_sessions, report_mode, min_days)
            if not baseline_data:
                continue
            baseline_values = [v for _, v in baseline_data]
        elif use_period_mode:
            # Current value = aggregate over the current period.
            current_period_data = [(d, v) for d, v in raw_data
                                   if period_start <= d <= period_end]
            current_period_values = [v for _, v in current_period_data]
            if current_period_values:
                if kpi_name in rate_metrics:
                    current_value = sum(current_period_values) / len(current_period_values)
                else:
                    # Count metrics ALWAYS display the raw period total
                    # (₹140,767 for a month, not a per-day average).
                    current_value = sum(current_period_values)
                    days_in_current = len(current_period_values)
            else:
                current_value = raw_data[-1][1]

            # Aggregate baseline into period windows
            if is_monthly:
                baseline_data = aggregate_monthly(baseline_data, use_average=(kpi_name in rate_metrics))
            else:
                baseline_data = aggregate_weekly(baseline_data, use_average=(kpi_name in rate_metrics))
            if not baseline_data:
                continue
            baseline_values = [v for _, v in baseline_data]
        else:
            # Daily mode — use last entry as current value
            current_value = raw_data[-1][1] if raw_data else 0.0

            if day_of_week_normalize and kpi_name not in rate_metrics:
                # Compare to same day of week
                dow_data = normalize_by_day_of_week(baseline_data, target_date)
                baseline_values = [v for _, v in dow_data] if len(dow_data) >= 3 else [v for _, v in baseline_data]
            else:
                baseline_values = [v for _, v in baseline_data]

        # Compute statistics.
        if is_monthly and days_in_current > 0:
            # Monthly count metrics: baseline windows are PER-DAY averages
            # (aggregate_monthly is length-invariant). Compute the z-score in
            # per-day space so a 31-day month isn't flagged just for being long,
            # then scale the baseline mean/stddev back into monthly-total units
            # so the DISPLAYED current_value (raw total) reconciles with them.
            mean_daily, stddev_daily = compute_rolling_stats(baseline_values)
            current_daily = current_value / days_in_current
            z = compute_z_score(current_daily, mean_daily, stddev_daily)
            mean = mean_daily * days_in_current
            stddev = stddev_daily * days_in_current
        else:
            mean, stddev = compute_rolling_stats(baseline_values)
            z = compute_z_score(current_value, mean, stddev)

        # Check suppression — override z-score to 0 for low-volume
        z_override = None
        status_override = None
        if should_suppress(kpi_name, sessions_count):
            z_override = 0.0
            status_override = AlertLevel.NORMAL
        else:
            # Use sensitivity-aware classification
            z_override = z
            status_override = classify_alert(z, sensitivity)

        metric = KPIMetric(
            name=kpi_name,
            current_value=current_value,
            baseline_mean=mean,
            standard_deviation=stddev,
            z_score_override=z_override,
            status_override=status_override,
        )

        metrics.append(metric)

    return metrics


def _query_computed_conversion(conn, start: str, end: str) -> list[tuple[str, float]]:
    """Compute daily conversion_rate as orders/sessions from stored KPIs."""
    orders_data = dict(query_kpi_range(conn, "orders", start, end))
    sessions_data = dict(query_kpi_range(conn, "sessions", start, end))
    result = []
    for d in sorted(set(orders_data) | set(sessions_data)):
        s = sessions_data.get(d, 0)
        o = orders_data.get(d, 0)
        if s > 0:
            result.append((d, o / s))
    return result


def compute_pop_change(
    conn,
    metric_name: str,
    reference_date: str,
    period_days: int = 7,
    boundaries: tuple[str, str, str, str] | None = None,
) -> tuple[float | None, float | None]:
    """Compute period-over-period change.

    If boundaries is provided, uses those dates directly.
    Otherwise falls back to last completed Mon-Sun weeks (legacy behavior).

    Returns (current_period_value, prior_period_value).
    Count metrics (revenue, orders, sessions) use sums.
    Rate metrics (conversion_rate, AOV) use averages.
    Conversion rate uses period-total method (sum orders / sum sessions)
    to avoid Simpson's paradox from averaging daily rates.
    """
    if boundaries:
        current_start, current_end, prior_start, prior_end = boundaries
    else:
        from .date_utils import week_boundaries
        current_start, current_end, prior_start, prior_end = week_boundaries(
            reference_date, "weekly"
        )

    if metric_name == "conversion_rate":
        # Period-total conversion: sum(orders) / sum(sessions) for each period.
        # Avoids Simpson's paradox from averaging daily rates.
        current_orders = query_kpi_range(conn, "orders", current_start, current_end)
        current_sessions = query_kpi_range(conn, "sessions", current_start, current_end)
        prior_orders = query_kpi_range(conn, "orders", prior_start, prior_end)
        prior_sessions = query_kpi_range(conn, "sessions", prior_start, prior_end)

        if not current_orders or not current_sessions or not prior_orders or not prior_sessions:
            return None, None

        cur_total_sessions = sum(v for _, v in current_sessions)
        pri_total_sessions = sum(v for _, v in prior_sessions)

        if cur_total_sessions == 0 or pri_total_sessions == 0:
            return None, None

        current_agg = sum(v for _, v in current_orders) / cur_total_sessions
        prior_agg = sum(v for _, v in prior_orders) / pri_total_sessions
        return current_agg, prior_agg

    current_data = query_kpi_range(conn, metric_name, current_start, current_end)
    prior_data = query_kpi_range(conn, metric_name, prior_start, prior_end)

    if not current_data or not prior_data:
        return None, None

    # Min-data guard: 1 day when boundaries are explicit (rolling mode),
    # 3 days for legacy fallback (weekly mode)
    min_days = 1 if boundaries else 3
    if len(current_data) < min_days or len(prior_data) < min_days:
        return None, None

    current_values = [v for _, v in current_data]
    prior_values = [v for _, v in prior_data]

    # Count/total metrics: use sums. Rate metrics (AOV): use averages.
    rate_metrics = {"average_order_value"}
    if metric_name in rate_metrics:
        current_agg = sum(current_values) / len(current_values)
        prior_agg = sum(prior_values) / len(prior_values)
    else:
        current_agg = sum(current_values)
        prior_agg = sum(prior_values)

    return current_agg, prior_agg


def compute_funnel_rates(
    conn,
    start_date: str,
    end_date: str,
) -> list[FunnelStep]:
    """Compute funnel step rates from DB data.

    Returns FunnelSteps for: Sessions, Cart Additions, Reached Checkout, Completed Checkout.
    """
    sessions_data = query_kpi_range(conn, "sessions", start_date, end_date)
    cart_data = query_kpi_range(conn, "sessions_with_cart_additions", start_date, end_date)
    checkout_data = query_kpi_range(conn, "sessions_that_reached_checkout", start_date, end_date)
    completed_data = query_kpi_range(conn, "sessions_that_completed_checkout", start_date, end_date)

    total_sessions = int(sum(v for _, v in sessions_data)) if sessions_data else 0
    total_cart = int(sum(v for _, v in cart_data)) if cart_data else 0
    total_checkout = int(sum(v for _, v in checkout_data)) if checkout_data else 0
    total_completed = int(sum(v for _, v in completed_data)) if completed_data else 0

    if total_sessions == 0:
        return []

    return [
        FunnelStep(label="Sessions", sessions=total_sessions, rate=1.0),
        FunnelStep(label="Cart Additions", sessions=total_cart, rate=total_cart / total_sessions),
        FunnelStep(label="Reached Checkout", sessions=total_checkout, rate=total_checkout / total_sessions),
        FunnelStep(label="Completed Checkout", sessions=total_completed, rate=total_completed / total_sessions),
    ]


def get_checkout_completion_rate(conn, target_date: str) -> float | None:
    """Calculate checkout completion rate: completed_checkout / sessions."""
    sessions = _get_sessions_for_date(conn, target_date)
    completed = query_kpi_range(
        conn, "sessions_that_completed_checkout", target_date, target_date
    )
    if sessions and sessions > 0 and completed:
        return completed[0][1] / sessions
    return None
