"""CLI orchestrator for Shopify AI Sentinel (developer/debug tool).

Reports are written to HTML files — nothing is emailed. Delivery is the
Claude host's job in MCP mode.

Usage:
    python -m sentinel.main --mode daily           # Daily anomaly check
    python -m sentinel.main --mode weekly          # Weekly briefing (Mon-Sun)
    python -m sentinel.main --mode rolling         # Mid-week update (WTD vs prior WTD)
    python -m sentinel.main --mode backtest        # Run backtest
    python -m sentinel.main --mode fetch-history --days 180
    python -m sentinel.main --store mystore --mode weekly  # Multi-store
    python -m sentinel.main --store mystore --mode purge --purge-older-than 365          # Dry-run purge
    python -m sentinel.main --store mystore --mode purge --purge-older-than 365 --confirm # Execute purge
    python -m sentinel.main --store mystore --mode audit-report                          # Full audit trail
    python -m sentinel.main --store mystore --mode audit-report --audit-filter purge     # Purge history only
"""

import argparse
import sys
from datetime import datetime

from .config import get_config, get_store_config, SentinelConfig
from .date_utils import week_boundaries, days_in_period, period_boundaries
from .db import init_db, get_connection, insert_audit_log
from .fetcher import fetch_sales, fetch_sessions, fetch_funnel
from .anomaly import analyze_kpis, compute_overall_status
from .models import AlertLevel
from .reasoning import analyze as llm_analyze
from .backtest import main as backtest_main
from . import services

_STATUS_ICONS = {"normal": "🟢", "warning": "🟡", "critical": "🔴"}


def daily_check(config: SentinelConfig, store_slug: str | None = None) -> None:
    """Run daily anomaly check.

    Fetches today's data, analyzes z-scores, runs LLM reasoning on criticals.
    """
    print(f"\n{'=' * 60}")
    print(f"AI Sentinel — Daily Check ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"{'=' * 60}")

    with get_connection(config.db_path) as conn:
        # Fetch latest data
        print("\n1. Fetching latest data...")
        try:
            fetch_sales(config, conn, since_days=2)
            fetch_sessions(config, conn, since_days=2)
            fetch_funnel(config, conn, since_days=2)
            insert_audit_log(conn, mode="daily", action="fetch_complete",
                             details={"days": 2}, store_slug=store_slug)
            conn.commit()
        except Exception as e:
            print(f"   Fetch error: {e}")
            insert_audit_log(conn, mode="daily", action="fetch_failed",
                             details={"days": 2, "error": str(e)}, store_slug=store_slug)
            conn.commit()

        # Analyze
        print("\n2. Analyzing KPIs...")
        today = datetime.now().strftime("%Y-%m-%d")
        metrics = analyze_kpis(conn, today, sensitivity=config.alert_sensitivity)

        if not metrics:
            print("   No metrics available for analysis.")
            return

        for m in metrics:
            status_icon = {"normal": "🟢", "warning": "🟡", "critical": "🔴"}.get(m.status.value, "⚪")
            print(f"   {status_icon} {m.name}: {m.current_value:.4f} (z={m.z_score:+.2f}, {m.status.value})")

        # Check for critical alerts
        critical_metrics = [m for m in metrics if m.status == AlertLevel.CRITICAL]
        warning_metrics = [m for m in metrics if m.status == AlertLevel.WARNING]

        if critical_metrics:
            print(f"\n3. CRITICAL anomalies detected! Running LLM reasoning...")
            # Daily mode uses rolling boundaries for context (today vs yesterday)
            daily_boundaries = week_boundaries(today, "rolling")
            context = services.build_store_context(conn, metrics, config, today, daily_boundaries)
            programmatic_status = compute_overall_status(metrics, context.pop_changes)
            print(f"   Programmatic status: {programmatic_status.value}")
            report = llm_analyze(
                config, context,
                fixed_status=programmatic_status,
                store_slug=store_slug,
                report_mode="daily",
                reference_date=today,
                db_conn=conn,
            )

            print(f"   Status: {report.overall_status.value}")
            print(f"   Insight: {report.primary_insight}")
            print(f"   Reasoning: {report.reasoning}")
            if report.recommended_action:
                print(f"   Action: {report.recommended_action}")

            insert_audit_log(conn, mode="daily", action="alert_detected",
                             details={"critical_count": len(critical_metrics)}, store_slug=store_slug)
            conn.commit()

        elif warning_metrics:
            print(f"\n3. {len(warning_metrics)} warning(s) detected (no critical alerts).")
            print("   Warnings will be included in the next weekly briefing.")

        else:
            print(f"\n3. All metrics normal. No alerts needed.")

    print(f"\n{'=' * 60}")
    print("Daily check complete.")


def _confirm_standing_notes(notes: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    """Interactively confirm which standing notes to include."""
    print("\n   Standing notes detected:")
    for i, (cat, weeks, text) in enumerate(notes, 1):
        print(f"     [{i}] {cat} ({weeks} weeks): {text[:80]}")

    try:
        choice = input("\n   Include all? [Y/n/select]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return notes

    if choice in ("", "y", "yes"):
        return notes
    if choice in ("n", "no"):
        print("   Skipping all standing notes.")
        return []
    if choice == "select":
        kept = []
        for i, note in enumerate(notes, 1):
            try:
                ans = input(f"     Include [{i}]? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                kept.append(note)
                continue
            if ans not in ("n", "no"):
                kept.append(note)
        return kept

    return notes


def weekly_briefing(
    config: SentinelConfig,
    store_slug: str | None = None,
    report_mode: str = "weekly",
    reference_date: str | None = None,
    compare: str = "mom",
    sales_channel_note: str = "",
) -> None:
    """Run weekly, rolling, or monthly briefing. Writes the report HTML to disk.

    report_mode controls date boundaries:
        "weekly"  — last completed Mon-Sun vs prior Mon-Sun (7v7)
        "rolling" — Mon of current week through today vs same days prior week (NvN)
        "monthly" — last completed calendar month vs prior month (compare="mom")
                    or same month last year (compare="yoy")
    """
    if reference_date is None:
        reference_date = datetime.now().strftime("%Y-%m-%d")

    boundaries = period_boundaries(reference_date, report_mode, compare)
    current_start, current_end, prior_start, prior_end = boundaries
    period_days = days_in_period(boundaries)
    mode_label = services.MODE_LABELS.get(report_mode, "Briefing")

    print(f"\n{'=' * 60}")
    print(f"AI Sentinel — {mode_label} ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"   Period: {current_start} → {current_end} ({period_days} days)")
    print(f"   Prior:  {prior_start} → {prior_end}")
    print(f"{'=' * 60}")

    print("\n1. Fetching + analyzing...")
    try:
        analysis = services.analyze_store(
            config, mode=report_mode, compare=compare, reference_date=reference_date,
            sync=True, store_slug=store_slug,
            on_stage=lambda ev: print(f"   {ev}"),
        )
    except services.NoData as e:
        print(f"   {e}")
        return
    for w in analysis.warnings:
        print(f"   ⚠ {w}")

    for m in analysis.metrics:
        icon = _STATUS_ICONS.get(m.status.value, "⚪")
        print(f"   {icon} {m.name}: {m.current_value:.4f} (z={m.z_score:+.2f}, {m.status.value})")
    print(f"   Programmatic status: {analysis.overall_status.value}")
    if analysis.active_insights:
        print(f"   Prior insights loaded: {len(analysis.active_insights)} active")

    print("\n2. Running LLM reasoning...")
    with get_connection(config.db_path) as conn:
        report = llm_analyze(
            config, analysis.context,
            prior_insights=analysis.active_insights or None,
            fixed_status=analysis.overall_status,
            store_slug=store_slug,
            report_mode=report_mode,
            reference_date=analysis.reference_date,
            db_conn=conn,
            compare=compare,
        )
        conn.commit()
    print(f"   Status: {report.overall_status.value}")
    print(f"   Insight: {report.primary_insight}")

    print(f"\n3. Building {report_mode} report...")
    result = services.render_report_html(
        config, report, mode=report_mode, compare=compare,
        store_slug=store_slug, sales_channel_note=sales_channel_note,
        analysis=analysis,
        confirm_standing_notes=_confirm_standing_notes if sys.stdin.isatty() else None,
    )
    for w in result.warnings[len(analysis.warnings):]:
        print(f"   ⚠ {w}")
    print(f"   Subject: {result.subject_line}")
    print(f"   Report saved to: {result.html_path}")

    print(f"\n{'=' * 60}")
    print(f"{mode_label} complete.")


def fetch_history(config: SentinelConfig, days: int = 180, store_slug: str | None = None) -> None:
    """Pull historical data into SQLite."""
    print(f"\n{'=' * 60}")
    print(f"AI Sentinel — Historical Data Fetch ({days} days)")
    print(f"{'=' * 60}")

    result = services.sync_store(config, days=days, store_slug=store_slug,
                                 on_stage=lambda ev: print(f"  {ev}"),
                                 audit_mode="fetch-history")

    print(f"\nFetch results:")
    for category, count in result.records.items():
        print(f"  {category}: {count} records")
    for w in result.warnings:
        print(f"  ⚠ {w}")
    if result.coverage[0]:
        print(f"  Coverage: {result.coverage[0]} → {result.coverage[1]}")


def purge(config: SentinelConfig, purge_older_than: int, confirm: bool = False, store_slug: str | None = None) -> None:
    """Purge data older than N days (minimum 365).

    Manual-only, triggered by client request. Logs all deletions.
    """
    print(f"\n{'=' * 60}")
    print(f"SENTINEL — Data Purge")
    print(f"{'=' * 60}")
    print(f"   Store DB:    {config.db_path}")

    try:
        result = services.purge_store(config, purge_older_than, confirm=confirm,
                                      store_slug=store_slug)
    except ValueError as e:
        print(f"\nError: {e}")
        sys.exit(1)

    print(f"   Cutoff:      {result.cutoff_date} ({purge_older_than} days ago)")
    if result.dry_run:
        print(f"\n   DRY RUN — showing what would be deleted:\n")
    else:
        print(f"\n   EXECUTED PURGE:\n")

    verb = "would delete" if result.dry_run else "deleted"
    for table, count in result.tables.items():
        print(f"   {table}: {verb} {count} row(s) older than {result.cutoff_date}")

    if result.dry_run:
        print(f"\n   To execute, re-run with --confirm")
    else:
        print(f"\n   Total rows purged: {result.total_rows}")

    print(f"\n{'=' * 60}")
    print("Purge complete.")


def audit_report(
    config: SentinelConfig,
    mode_filter: str | None = None,
    limit: int = 200,
) -> None:
    """Generate a human-readable audit report to stdout."""
    print(f"\n{'=' * 60}")
    print(f"SENTINEL — Audit Report")
    print(f"{'=' * 60}")
    print(f"   Store DB: {config.db_path}")
    if mode_filter:
        print(f"   Filter:   {mode_filter}")
    print(f"   Limit:    {limit} entries")
    print()

    rows = services.get_audit_log(config, mode_filter=mode_filter, limit=limit)

    if not rows:
        print("   No audit log entries found.")
        print(f"\n{'=' * 60}")
        return

    # Summary
    purge_rows = [r for r in rows if r["mode"] == "purge" and r["action"] == "purge_executed"]
    print(f"   Total entries: {len(rows)}")
    print(f"   Purge executions: {len(purge_rows)}")
    print()

    # Detail table
    print(f"   {'Timestamp':<22} {'Operator':<14} {'Mode':<16} {'Action'}")
    print(f"   {'-'*22} {'-'*14} {'-'*16} {'-'*30}")
    for row in rows:
        ts = row["timestamp"][:19]
        print(f"   {ts:<22} {row['operator']:<14} {row['mode']:<16} {row['action']}")
        details = row.get("details")
        if isinstance(details, dict):
            for k, v in details.items():
                print(f"   {'':>22}    {k}: {v}")

    print(f"\n{'=' * 60}")
    print("Audit report complete.")


def run(config: SentinelConfig | None = None, mode: str = "daily", days: int = 180, purge_older_than: int = 0, confirm: bool = False, store_slug: str | None = None, audit_filter: str | None = None, audit_limit: int = 200, compare: str = "mom", sales_channel_note: str = "") -> None:
    """Main entry point."""
    if config is None:
        config = get_config()

    # Ensure DB exists
    init_db(config.db_path)

    if mode == "daily":
        daily_check(config, store_slug=store_slug)
    elif mode in ("weekly", "rolling", "monthly"):
        weekly_briefing(config, store_slug=store_slug, report_mode=mode,
                        compare=compare, sales_channel_note=sales_channel_note)
    elif mode == "backtest":
        backtest_main()
    elif mode == "fetch-history":
        fetch_history(config, days=days, store_slug=store_slug)
    elif mode == "purge":
        if purge_older_than <= 0:
            print("Error: --purge-older-than N is required for purge mode (min 365).")
            sys.exit(1)
        purge(config, purge_older_than=purge_older_than, confirm=confirm, store_slug=store_slug)
    elif mode == "audit-report":
        audit_report(config, mode_filter=audit_filter, limit=audit_limit)
    else:
        print(f"Unknown mode: {mode}")
        sys.exit(1)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Shopify AI Sentinel — Automated Store Intelligence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  daily          Run daily anomaly check
  weekly         Generate weekly briefing (last completed Mon-Sun vs prior Mon-Sun)
  rolling        Generate mid-week update (WTD Mon-today vs same days prior week)
  monthly        Generate monthly briefing (last completed calendar month; --compare mom|yoy)
  backtest       Replay historical data to validate thresholds
  fetch-history  Pull historical data into SQLite
  purge          Delete data older than N days (client-requested, manual only)
  audit-report   Show audit trail of all actions for a store

Examples:
  python -m sentinel.main --mode weekly
  python -m sentinel.main --mode rolling
  python -m sentinel.main --mode monthly
  python -m sentinel.main --mode monthly --compare yoy
  python -m sentinel.main --store mystore --mode weekly
  python -m sentinel.main --store mystore --mode rolling
  python -m sentinel.main --store mystore --mode purge --purge-older-than 365
  python -m sentinel.main --store mystore --mode purge --purge-older-than 365 --confirm
  python -m sentinel.main --store mystore --mode audit-report
  python -m sentinel.main --store mystore --mode audit-report --audit-filter purge
        """,
    )
    parser.add_argument(
        "--store",
        type=str,
        default=None,
        help="Store slug from stores.json (e.g. mystore). If omitted, loads config from .env.",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "rolling", "monthly", "backtest", "fetch-history", "purge", "audit-report"],
        default="daily",
        help="Operation mode (default: daily)",
    )
    parser.add_argument(
        "--compare",
        choices=["mom", "yoy"],
        default="mom",
        help="Monthly comparison basis: mom (prior month) or yoy (same month last year). Used with --mode monthly.",
    )
    parser.add_argument(
        "--sales-channel-note",
        type=str,
        default="",
        help="Per-run insight note shown under the monthly Sales Channel Breakdown table.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Days of history to fetch (default: 180, for fetch-history mode)",
    )
    parser.add_argument(
        "--purge-older-than",
        type=int,
        default=0,
        help="Purge data older than N days (min 365, used with --mode purge)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm purge execution (without this flag, purge runs as dry-run)",
    )
    parser.add_argument(
        "--audit-filter",
        type=str,
        default=None,
        choices=["purge", "daily", "weekly", "rolling", "monthly", "fetch-history"],
        help="Filter audit report to a specific mode (default: show all)",
    )
    parser.add_argument(
        "--audit-limit",
        type=int,
        default=200,
        help="Maximum number of audit log entries to show (default: 200)",
    )

    args = parser.parse_args()

    config = None
    if args.store:
        try:
            config = get_store_config(args.store)
        except (FileNotFoundError, KeyError) as e:
            print(f"Error: {e}")
            sys.exit(1)

    run(config=config, mode=args.mode, days=args.days,
        purge_older_than=args.purge_older_than,
        confirm=args.confirm, store_slug=args.store,
        audit_filter=args.audit_filter, audit_limit=args.audit_limit,
        compare=args.compare, sales_channel_note=args.sales_channel_note)


if __name__ == "__main__":
    main()
