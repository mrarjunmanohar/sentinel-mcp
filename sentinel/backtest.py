"""Backtesting harness for Shopify AI Sentinel.

Replays historical data day-by-day to validate z-score thresholds
and tune sensitivity for a low-volume store.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import AlertLevel, KPIMetric, AIAnalystReport, StoreContext, TrafficSource
from .anomaly import analyze_kpis, CORE_KPIS
from .db import query_kpi_range, query_recent_events, get_connection
from .reasoning import analyze as llm_analyze
from .config import SentinelConfig, get_config


@dataclass
class BacktestResult:
    """Result of a single day's backtest."""
    date: str
    alerts_fired: list[KPIMetric] = field(default_factory=list)
    report: AIAnalystReport | None = None

    @property
    def has_alerts(self) -> bool:
        return len(self.alerts_fired) > 0

    @property
    def max_severity(self) -> AlertLevel:
        if not self.alerts_fired:
            return AlertLevel.NORMAL
        severities = [m.status for m in self.alerts_fired]
        if AlertLevel.CRITICAL in severities:
            return AlertLevel.CRITICAL
        if AlertLevel.WARNING in severities:
            return AlertLevel.WARNING
        return AlertLevel.NORMAL


def run_backtest(
    db_path: str,
    config: SentinelConfig,
    start_date: str,
    end_date: str,
    use_llm: bool = False,
    weekly_mode: bool = False,
    sensitivity: str = "MED",
) -> list[BacktestResult]:
    """Run backtest over a date range.

    For each day: computes z-scores using only prior 30 days of data.
    Optionally invokes LLM for reasoning (slow, costs money).

    Args:
        db_path: Path to SQLite database
        config: SentinelConfig instance
        start_date: Start of backtest range (YYYY-MM-DD)
        end_date: End of backtest range (YYYY-MM-DD)
        use_llm: If True, run LLM reasoning for days with alerts
        weekly_mode: If True, use weekly aggregation
        sensitivity: Alert sensitivity (LOW/MED/HIGH)

    Returns:
        List of BacktestResult for each day
    """
    results = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    total_days = (end - current).days + 1
    print(f"\nBacktest: {start_date} to {end_date} ({total_days} days)")
    print(f"Settings: weekly_mode={weekly_mode}, sensitivity={sensitivity}, use_llm={use_llm}")
    print("-" * 60)

    with get_connection(db_path) as conn:
        day_num = 0
        while current <= end:
            day_num += 1
            date_str = current.strftime("%Y-%m-%d")

            # Analyze KPIs for this date
            metrics = analyze_kpis(
                conn, date_str, weekly_mode=weekly_mode, sensitivity=sensitivity
            )

            # Find alerts (non-NORMAL metrics)
            alerts = [m for m in metrics if m.status != AlertLevel.NORMAL]

            result = BacktestResult(date=date_str, alerts_fired=alerts)

            # Optionally run LLM reasoning
            if use_llm and alerts:
                events = query_recent_events(conn, days=7)
                context = StoreContext(
                    metrics=metrics,
                    recent_events=events,
                    top_traffic_sources=[],
                )
                try:
                    result.report = llm_analyze(config, context)
                except Exception as e:
                    print(f"  LLM error on {date_str}: {e}")

            results.append(result)

            # Progress output
            if alerts:
                alert_summary = ", ".join(
                    f"{m.name}={m.status.value}(z={m.z_score:+.1f})" for m in alerts
                )
                print(f"  [{day_num}/{total_days}] {date_str}: {result.max_severity.value.upper()} — {alert_summary}")
            elif day_num % 7 == 0:
                print(f"  [{day_num}/{total_days}] {date_str}: normal")

            current += timedelta(days=1)

    return results


def print_backtest_summary(results: list[BacktestResult]) -> None:
    """Print a summary table of backtest results."""
    total = len(results)
    normal = sum(1 for r in results if r.max_severity == AlertLevel.NORMAL)
    warnings = sum(1 for r in results if r.max_severity == AlertLevel.WARNING)
    criticals = sum(1 for r in results if r.max_severity == AlertLevel.CRITICAL)

    print(f"\n{'=' * 60}")
    print("BACKTEST SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total days:     {total}")
    print(f"  Normal days:    {normal} ({normal/total*100:.0f}%)")
    print(f"  Warning days:   {warnings} ({warnings/total*100:.0f}%)")
    print(f"  Critical days:  {criticals} ({criticals/total*100:.0f}%)")

    if warnings + criticals > 0:
        print(f"\n  Alert rate: {(warnings + criticals) / total * 100:.1f}% of days")

    # Per-KPI breakdown
    kpi_alerts: dict[str, dict[str, int]] = {}
    for r in results:
        for m in r.alerts_fired:
            if m.name not in kpi_alerts:
                kpi_alerts[m.name] = {"warning": 0, "critical": 0}
            kpi_alerts[m.name][m.status.value] += 1

    if kpi_alerts:
        print(f"\n  Per-KPI Alert Breakdown:")
        for kpi, counts in sorted(kpi_alerts.items()):
            print(f"    {kpi}: {counts['warning']} warnings, {counts['critical']} criticals")

    # Timeline of critical alerts
    critical_days = [r for r in results if r.max_severity == AlertLevel.CRITICAL]
    if critical_days:
        print(f"\n  Critical Alert Timeline:")
        for r in critical_days[:20]:  # Show first 20
            alert_names = ", ".join(m.name for m in r.alerts_fired if m.status == AlertLevel.CRITICAL)
            print(f"    {r.date}: {alert_names}")

    print(f"{'=' * 60}")


def tune_thresholds(results: list[BacktestResult]) -> dict[str, dict]:
    """Analyze backtest results and suggest optimal thresholds.

    Goal: 5-15% of days should trigger warnings, <5% criticals.
    If too many alerts → raise thresholds. Too few → lower them.
    """
    total = len(results)
    if total == 0:
        return {}

    warning_rate = sum(1 for r in results if r.max_severity == AlertLevel.WARNING) / total
    critical_rate = sum(1 for r in results if r.max_severity == AlertLevel.CRITICAL) / total
    alert_rate = warning_rate + critical_rate

    suggestions = {
        "current_rates": {
            "warning_rate": f"{warning_rate:.1%}",
            "critical_rate": f"{critical_rate:.1%}",
            "total_alert_rate": f"{alert_rate:.1%}",
        },
        "recommendations": [],
    }

    if alert_rate > 0.30:
        suggestions["recommendations"].append(
            "Alert rate too high (>30%). Recommend switching to LOW sensitivity "
            "or enabling weekly_mode to smooth noise."
        )
    elif alert_rate > 0.15:
        suggestions["recommendations"].append(
            "Alert rate slightly high (>15%). Consider LOW sensitivity for non-critical KPIs."
        )
    elif alert_rate < 0.02:
        suggestions["recommendations"].append(
            "Very few alerts (<2%). Consider HIGH sensitivity to catch more anomalies."
        )
    else:
        suggestions["recommendations"].append(
            f"Alert rate at {alert_rate:.1%} — within target range (2-15%)."
        )

    if critical_rate > 0.10:
        suggestions["recommendations"].append(
            "Critical rate >10%. Enable weekly_mode to reduce false criticals from daily noise."
        )

    return suggestions


def main():
    """Run a full backtest with default settings."""
    config = get_config()

    # Default: backtest the last 90 days (need 30 days lead-in for baseline)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    print("Shopify AI Sentinel — Backtesting Harness")
    print(f"Database: {config.db_path}")

    # Run with daily mode first
    print("\n=== DAILY MODE (MED sensitivity) ===")
    daily_results = run_backtest(
        config.db_path, config, start_date, end_date,
        weekly_mode=False, sensitivity="MED",
    )
    print_backtest_summary(daily_results)
    daily_suggestions = tune_thresholds(daily_results)

    # Run with weekly mode
    print("\n=== WEEKLY MODE (MED sensitivity) ===")
    weekly_results = run_backtest(
        config.db_path, config, start_date, end_date,
        weekly_mode=True, sensitivity="MED",
    )
    print_backtest_summary(weekly_results)
    weekly_suggestions = tune_thresholds(weekly_results)

    # Print recommendations
    print("\n" + "=" * 60)
    print("TUNING RECOMMENDATIONS")
    print("=" * 60)
    print("\nDaily mode:")
    for rec in daily_suggestions.get("recommendations", []):
        print(f"  → {rec}")
    print("\nWeekly mode:")
    for rec in weekly_suggestions.get("recommendations", []):
        print(f"  → {rec}")


if __name__ == "__main__":
    main()
