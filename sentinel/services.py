"""Service layer for Shopify AI Sentinel.

Print-free, exception-raising orchestration shared by the CLI (main.py) and
the MCP server (mcp_server.py). Long operations expose sync generators of
StageEvents (see progress.py); nothing here writes to stdout — fetcher
warnings are collected via logging and returned in results.
"""

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .anomaly import CORE_KPIS, analyze_kpis, compute_overall_status, compute_pop_change
from .calculations import (
    compute_inventory_turnover,
    compute_pct_of_total,
    compute_return_rate,
    compute_wow_change,
)
from .config import SentinelConfig, get_config
from .date_utils import days_in_period, fetch_since_days, period_boundaries
from .db import (
    get_connection,
    init_db,
    insert_audit_log,
    purge_old_data,
    query_active_insights,
    query_audit_log,
    query_channel_performance,
    query_kpi_range,
    query_recent_events,
    query_top_products,
    save_and_reconcile_insights,
)
from .email_template import (
    CURRENCY_SYMBOLS,
    build_report_subject,
    generate_email_html,
    generate_store_briefing,
)
from .fetcher import fetch_funnel, fetch_sales, fetch_sessions, iter_fetch_stages, iter_history_stages
from .guardrails import sanitize_campaign_action
from .models import (
    AIAnalystReport,
    AlertLevel,
    ChannelPerformance,
    InventoryOps,
    KPIMetric,
    ProductPerformance,
    StoreContext,
    TrafficSource,
)
from .paths import db_path_for, reports_dir, stores_path
from .report_builder import build_store_report

MODE_LABELS = {
    "weekly": "Weekly Briefing",
    "rolling": "Mid-Week Update",
    "monthly": "Monthly Briefing",
}


# ─── Exceptions ───────────────────────────────────────────────────────────────

class SentinelError(Exception):
    """Base class for service-layer errors."""


class StoreNotConfigured(SentinelError):
    """The requested store slug has no configuration."""


class NoData(SentinelError):
    """Not enough stored data to run the requested analysis."""


class FetchFailed(SentinelError):
    """The Shopify fetch failed outright (not just per-stage warnings)."""


class TokenInvalid(SentinelError):
    """The Shopify Admin API token is missing, wrong, or lacks a scope."""


# ─── Results ──────────────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    records: dict[str, int]
    warnings: list[str]
    coverage: tuple[str | None, str | None]  # (earliest, latest) kpi date


@dataclass
class HistoryChunkResult:
    fetched_range: tuple[str, str] | None  # (start, end) of the window this call fetched
    records: dict[str, int]
    warnings: list[str]
    days_of_history: int
    remaining_days: int
    done: bool


@dataclass
class AnalysisResult:
    store_slug: str | None
    mode: str
    compare: str
    reference_date: str
    boundaries: tuple[str, str, str, str]  # current_start, current_end, prior_start, prior_end
    period_days: int
    metrics: list[KPIMetric]
    context: StoreContext
    overall_status: AlertLevel
    status_reason: str
    active_insights: list[dict]
    coverage: tuple[str | None, str | None]
    warnings: list[str] = field(default_factory=list)

    @property
    def stale(self) -> bool:
        latest = self.coverage[1]
        return bool(latest and latest < self.boundaries[1])

    def to_payload(self, config: SentinelConfig) -> dict:
        """Compact JSON-safe dict sized for LLM context (tables capped)."""
        cur_start, cur_end, prior_start, prior_end = self.boundaries
        kpis = []
        for m in self.metrics:
            kpis.append({
                "name": m.name,
                "current": round(m.current_value, 4),
                "baseline_mean": round(m.baseline_mean, 4),
                "z_score": round(m.z_score, 2),
                "status": m.status.value,
                "pop_change": _round_or_none(self.context.pop_changes.get(m.name), 4),
            })
        products = [{
            "title": p.product_title,
            "units_sold": p.units_sold,
            "revenue": round(p.revenue, 2),
            "pct_total_revenue": round(p.pct_total_revenue, 3),
            "pop_change": _round_or_none(p.wow_change, 3),
        } for p in self.context.top_products[:5]]
        channels = [{
            "source": c.source,
            "sessions": c.sessions,
            "revenue": round(c.revenue, 2),
            # No order-level channel attribution exists — expose rev/session,
            # never Shopify's per-channel conversion_rate (see CLAUDE.md).
            "rev_per_session": round(c.revenue / c.sessions, 2) if c.sessions else None,
            "sessions_pop_change": _round_or_none(c.wow_change, 3),
        } for c in self.context.channels[:5]]
        events = [{
            "type": e.event_type,
            "description": e.description,
            "timestamp": e.timestamp,
        } for e in self.context.recent_events[:10]]
        inv = self.context.inventory_ops
        payload = {
            "store": config.store_name or config.shop,
            "currency": config.currency,
            "mode": self.mode,
            "period": {
                "start": cur_start, "end": cur_end,
                "prior_start": prior_start, "prior_end": prior_end,
                "days": self.period_days,
            },
            "overall_status": self.overall_status.value,
            "status_reason": self.status_reason,
            "kpis": kpis,
            "top_products": products,
            "channels": channels,
            "inventory_ops": inv.model_dump() if inv else None,
            "recent_store_events": events,
            "standing_insights": [
                {"category": i.get("category"), "insight": i.get("insight_text"),
                 "consecutive_weeks": i.get("consecutive_weeks")}
                for i in self.active_insights[:5]
            ],
            "data_quality": {
                "latest_data_date": self.coverage[1],
                "stale": self.stale,
                "warnings": self.warnings,
            },
        }
        if self.mode == "monthly":
            payload["compare"] = self.compare
        return payload


@dataclass
class RenderResult:
    html_path: str
    subject_line: str
    markdown_digest: str
    email_recipients: list[str]
    period_start: str
    period_end: str
    html: str = ""        # the full report HTML (also written to html_path)
    email_html: str = ""  # compact (~10KB) variant sized for connector drafts
    warnings: list[str] = field(default_factory=list)


@dataclass
class QuickCheckResult:
    date: str
    overall_status: AlertLevel
    alerts: list[dict]
    normal_count: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class StoreStatus:
    slug: str
    shop: str
    store_name: str
    currency: str
    source: str  # "stores.json" or "env"
    latest_data_date: str | None
    days_of_history: int
    db_size_mb: float


@dataclass
class PurgeResult:
    cutoff_date: str
    dry_run: bool
    tables: dict[str, int]
    total_rows: int


@dataclass
class SetupResult:
    slug: str
    shop: str
    store_name: str
    currency: str
    token_source: str  # "argument" or "env"
    next_step: str


def _round_or_none(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


# ─── Fetcher warning capture ──────────────────────────────────────────────────

class _WarningCollector(logging.Handler):
    def __init__(self, sink: list[str]):
        super().__init__(level=logging.WARNING)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record.getMessage())


@contextmanager
def _collect_fetch_warnings(sink: list[str]):
    handler = _WarningCollector(sink)
    fetch_logger = logging.getLogger("sentinel.fetcher")
    fetch_logger.addHandler(handler)
    try:
        yield
    finally:
        fetch_logger.removeHandler(handler)


def _kpi_coverage(conn) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM kpi_snapshots "
        "WHERE metric_name = 'total_sales' AND date != 'aggregate'"
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


# ─── Store context assembly (shared by CLI daily mode and analyze_store) ─────

def get_sparkline_data(conn, metrics, reference_date: str, days: int = 30) -> dict[str, list[float]]:
    """Get sparkline data (last N days) for each metric."""
    start_dt = datetime.strptime(reference_date, "%Y-%m-%d") - timedelta(days=days)
    start_date = start_dt.strftime("%Y-%m-%d")

    sparklines = {}
    for m in metrics:
        data = query_kpi_range(conn, m.name, start_date, reference_date)
        sparklines[m.name] = [v for _, v in data]

    return sparklines


def _get_traffic_sources(conn, start_date: str) -> list[TrafficSource]:
    """Get top traffic sources from DB since start_date."""
    cursor = conn.execute(
        "SELECT metric_name, SUM(value) as total_sessions "
        "FROM kpi_snapshots WHERE metric_name LIKE 'traffic_%' "
        "AND date >= ? "
        "GROUP BY metric_name ORDER BY total_sessions DESC LIMIT 5",
        (start_date,),
    )

    sources = []
    for row in cursor.fetchall():
        parts = row[0].replace("traffic_", "").split("_", 1)
        source = parts[0] if parts else "unknown"
        name = parts[1] if len(parts) > 1 else "unknown"
        sources.append(TrafficSource(
            source=source,
            name=name,
            sessions=int(row[1]),
        ))

    return sources


def _compute_pop_changes(conn, metrics, reference_date: str, boundaries: tuple[str, str, str, str]) -> dict[str, float]:
    """Compute period-over-period PoP changes for all metrics using canonical boundaries."""
    pop_changes = {}
    for m in metrics:
        current_avg, prior_avg = compute_pop_change(conn, m.name, reference_date, boundaries=boundaries)
        if current_avg is not None and prior_avg is not None and prior_avg != 0:
            pop_changes[m.name] = (current_avg - prior_avg) / prior_avg
    return pop_changes


def build_store_context(conn, metrics, config, reference_date: str, boundaries: tuple[str, str, str, str]) -> StoreContext:
    """Build StoreContext from current data, including product/channel/inventory.

    Uses canonical boundaries from date_utils — never computes its own date ranges.
    """
    current_start, current_end, prior_start, prior_end = boundaries

    events = query_recent_events(conn, days=14)
    traffic = _get_traffic_sources(conn, current_start)
    pop_changes = _compute_pop_changes(conn, metrics, reference_date, boundaries)

    # Product performance
    current_products = query_top_products(conn, current_start, current_end, limit=5)
    prior_products = query_top_products(conn, prior_start, prior_end, limit=50)
    prior_by_title = {p["product_title"]: p["revenue"] for p in prior_products}
    total_rev = sum(p["revenue"] for p in current_products) or 1.0

    top_products = [
        ProductPerformance(
            product_title=p["product_title"],
            units_sold=p["units_sold"],
            revenue=p["revenue"],
            pct_total_revenue=compute_pct_of_total(p["revenue"], total_rev),
            wow_change=compute_wow_change(p["revenue"], prior_by_title.get(p["product_title"])),
        )
        for p in current_products
    ]

    # Channel performance
    current_channels = query_channel_performance(conn, current_start, current_end)
    prior_channels = query_channel_performance(conn, prior_start, prior_end)
    prior_by_ch = {(c["source"], c["name"]): c["sessions"] for c in prior_channels}

    channels = [
        ChannelPerformance(
            source=c["source"], name=c["name"],
            sessions=c["sessions"], revenue=c["revenue"],
            conversion_rate=c["conversion_rate"],
            wow_change=compute_wow_change(c["sessions"], prior_by_ch.get((c["source"], c["name"]))),
        )
        for c in current_channels[:5]
    ]

    # Inventory & operations
    def _latest_kpi(name):
        data = query_kpi_range(conn, name, current_start, current_end)
        return sum(v for _, v in data) if data else 0.0

    total_returns = _latest_kpi("return_count")
    metrics_by_name = {m.name: m for m in metrics}
    total_orders = metrics_by_name.get("orders")
    total_orders_val = total_orders.current_value if total_orders else 0
    returning = _latest_kpi("returning_customers")
    first_time = _latest_kpi("first_time_customers")
    inv_data = query_kpi_range(conn, "total_inventory_qty", reference_date, reference_date)
    total_inv = inv_data[0][1] if inv_data else 0
    active_skus_data = query_kpi_range(conn, "active_sku_count", reference_date, reference_date)
    active_skus = int(active_skus_data[0][1]) if active_skus_data else 0
    total_units = sum(p.units_sold for p in top_products)

    inventory_ops = InventoryOps(
        return_rate=compute_return_rate(total_returns, total_orders_val),
        inventory_turnover=compute_inventory_turnover(total_units, total_inv),
        active_sku_count=active_skus,
        zero_sales_sku_count=max(0, active_skus - len(current_products)),
        returning_customer_pct=(returning / (returning + first_time)) if (returning + first_time) > 0 else 0,
        first_time_customers=int(first_time),
        returning_customers=int(returning),
    )

    return StoreContext(
        metrics=metrics,
        recent_events=events,
        top_traffic_sources=traffic,
        pop_changes=pop_changes,
        top_products=top_products,
        channels=channels,
        inventory_ops=inventory_ops,
    )


# ─── Sync ─────────────────────────────────────────────────────────────────────

def sync_events(config: SentinelConfig, days: int = 30, store_slug: str | None = None,
                audit_mode: str = "sync"):
    """Generator: yields a StageEvent per fetch stage; returns a SyncResult.

    Raises FetchFailed if the fetch aborts (per-stage soft failures surface
    as warnings in the SyncResult instead).
    """
    init_db(config.db_path)
    warnings: list[str] = []
    records: dict[str, int] = {}
    with _collect_fetch_warnings(warnings):
        with get_connection(config.db_path) as conn:
            try:
                for ev in iter_fetch_stages(config, conn, since_days=days):
                    records[ev.name] = ev.records
                    yield ev
            except Exception as e:
                insert_audit_log(conn, mode=audit_mode, action="fetch_failed",
                                 details={"days": days, "error": str(e)}, store_slug=store_slug)
                conn.commit()
                raise FetchFailed(str(e)) from e
            insert_audit_log(conn, mode=audit_mode, action="fetch_complete",
                             details={"days": days, "records": records}, store_slug=store_slug)
            conn.commit()
            coverage = _kpi_coverage(conn)
    return SyncResult(records=records, warnings=warnings, coverage=coverage)


def sync_store(config: SentinelConfig, days: int = 30, store_slug: str | None = None,
               on_stage=None, audit_mode: str = "sync") -> SyncResult:
    """Run the full fetch, invoking on_stage(StageEvent) as stages complete."""
    gen = sync_events(config, days=days, store_slug=store_slug, audit_mode=audit_mode)
    while True:
        try:
            ev = next(gen)
        except StopIteration as stop:
            return stop.value
        if on_stage:
            on_stage(ev)


def history_chunk_events(config: SentinelConfig, total_days: int = 180,
                         chunk_days: int = 45, store_slug: str | None = None):
    """Generator: backfill ONE chunk of history (oldest-missing window first);
    yields StageEvents, returns a HistoryChunkResult.

    Each call is bounded (~chunk_days of data) so it fits inside an MCP tool
    timeout; callers loop until result.done. Assumes stored history is
    contiguous from its earliest date to today.
    """
    init_db(config.db_path)
    today = datetime.now().date()
    warnings: list[str] = []
    records: dict[str, int] = {}

    with _collect_fetch_warnings(warnings):
        with get_connection(config.db_path) as conn:
            earliest, _ = _kpi_coverage(conn)
            days_present = 0
            if earliest:
                days_present = (today - datetime.strptime(earliest, "%Y-%m-%d").date()).days

            if days_present >= total_days:
                return HistoryChunkResult(
                    fetched_range=None, records={}, warnings=warnings,
                    days_of_history=days_present, remaining_days=0, done=True,
                )

            until_days = days_present
            since_days = min(total_days, days_present + chunk_days)
            try:
                for ev in iter_history_stages(config, conn, since_days, until_days):
                    records[ev.name] = ev.records
                    yield ev
            except Exception as e:
                insert_audit_log(conn, mode="fetch-history", action="fetch_failed",
                                 details={"since_days": since_days, "until_days": until_days,
                                          "error": str(e)}, store_slug=store_slug)
                conn.commit()
                raise FetchFailed(str(e)) from e
            insert_audit_log(conn, mode="fetch-history", action="fetch_complete",
                             details={"since_days": since_days, "until_days": until_days,
                                      "records": records}, store_slug=store_slug)
            conn.commit()
            earliest_after, _ = _kpi_coverage(conn)

    new_days = 0
    if earliest_after:
        new_days = (today - datetime.strptime(earliest_after, "%Y-%m-%d").date()).days
    # A chunk that adds no older data means the store has no further history —
    # stop rather than loop forever against an empty window.
    made_progress = new_days > days_present
    remaining = max(0, total_days - new_days) if made_progress else 0
    fetched_start = (today - timedelta(days=since_days)).strftime("%Y-%m-%d")
    fetched_end = (today - timedelta(days=until_days)).strftime("%Y-%m-%d")
    return HistoryChunkResult(
        fetched_range=(fetched_start, fetched_end),
        records=records,
        warnings=warnings,
        days_of_history=new_days,
        remaining_days=remaining,
        done=remaining == 0,
    )


def sync_history_chunk(config: SentinelConfig, total_days: int = 180, chunk_days: int = 45,
                       store_slug: str | None = None, on_stage=None) -> HistoryChunkResult:
    """Run one backfill chunk, invoking on_stage(StageEvent) as stages complete."""
    gen = history_chunk_events(config, total_days=total_days, chunk_days=chunk_days,
                               store_slug=store_slug)
    while True:
        try:
            ev = next(gen)
        except StopIteration as stop:
            return stop.value
        if on_stage:
            on_stage(ev)


# ─── Analysis ─────────────────────────────────────────────────────────────────

def _status_reason(metrics: list[KPIMetric], pop_changes: dict[str, float],
                   overall: AlertLevel) -> str:
    critical = [m.name for m in metrics if m.status == AlertLevel.CRITICAL]
    warning = [m.name for m in metrics if m.status == AlertLevel.WARNING]
    drops = [f"{k} {v:+.0%}" for k, v in pop_changes.items()
             if k in CORE_KPIS and v <= -0.30]
    if overall == AlertLevel.CRITICAL:
        return f"critical z-score anomaly in: {', '.join(critical)}"
    if overall == AlertLevel.WARNING:
        parts = []
        if warning:
            parts.append(f"z-score warning in: {', '.join(warning)}")
        if drops:
            parts.append(f"PoP drop > 30%: {', '.join(drops)}")
        return "; ".join(parts) or "warning"
    return "all KPIs within normal range"


def analyze_store(config: SentinelConfig, mode: str = "weekly", compare: str = "mom",
                  reference_date: str | None = None, sync: bool = True,
                  store_slug: str | None = None, on_stage=None) -> AnalysisResult:
    """Fetch (optionally) + score + assemble the full quantitative analysis.

    Never calls an LLM. Raises NoData when the DB can't support the request.
    A failed live fetch degrades to a stale-data warning, not an error —
    the caller sees it in AnalysisResult.warnings / .stale.
    """
    if mode not in ("weekly", "rolling", "monthly"):
        raise ValueError(f"Unsupported analysis mode: {mode}")
    if reference_date is None:
        reference_date = datetime.now().strftime("%Y-%m-%d")

    boundaries = period_boundaries(reference_date, mode, compare)
    period_days = days_in_period(boundaries)
    init_db(config.db_path)

    warnings: list[str] = []
    if sync:
        since_days = fetch_since_days(reference_date, mode, compare)
        try:
            sync_result = sync_store(config, days=since_days, store_slug=store_slug,
                                     on_stage=on_stage, audit_mode=mode)
            warnings.extend(sync_result.warnings)
        except FetchFailed as e:
            warnings.append(f"Live data fetch failed — analysis uses stored data only: {e}")

    with get_connection(config.db_path) as conn:
        if mode == "monthly" and compare == "yoy":
            prior_start, prior_end = boundaries[2], boundaries[3]
            if not query_kpi_range(conn, "total_sales", prior_start, prior_end):
                raise NoData(
                    f"No stored data for the year-ago month {prior_start} → {prior_end}. "
                    "YoY needs pre-backfilled history — run a 730-day history sync first."
                )

        metrics = analyze_kpis(conn, reference_date, weekly_mode=True, report_mode=mode,
                               sensitivity=config.alert_sensitivity)
        if not metrics:
            raise NoData(
                f"No KPI data stored for {config.store_name or config.shop}. "
                "Run a history sync (e.g. 180 days) before analyzing."
            )

        context = build_store_context(conn, metrics, config, reference_date, boundaries)
        overall = compute_overall_status(metrics, context.pop_changes)
        active_insights = query_active_insights(conn)
        coverage = _kpi_coverage(conn)

    return AnalysisResult(
        store_slug=store_slug,
        mode=mode,
        compare=compare,
        reference_date=reference_date,
        boundaries=boundaries,
        period_days=period_days,
        metrics=metrics,
        context=context,
        overall_status=overall,
        status_reason=_status_reason(metrics, context.pop_changes, overall),
        active_insights=active_insights,
        coverage=coverage,
        warnings=warnings,
    )


def run_quick_check(config: SentinelConfig, store_slug: str | None = None) -> QuickCheckResult:
    """Daily z-score pass over the core KPIs. Cheap 2-day fetch, no LLM."""
    today = datetime.now().strftime("%Y-%m-%d")
    init_db(config.db_path)
    warnings: list[str] = []

    with _collect_fetch_warnings(warnings):
        with get_connection(config.db_path) as conn:
            try:
                fetch_sales(config, conn, since_days=2)
                fetch_sessions(config, conn, since_days=2)
                fetch_funnel(config, conn, since_days=2)
                insert_audit_log(conn, mode="daily", action="fetch_complete",
                                 details={"days": 2}, store_slug=store_slug)
            except Exception as e:
                warnings.append(f"Live data fetch failed — check uses stored data only: {e}")
                insert_audit_log(conn, mode="daily", action="fetch_failed",
                                 details={"days": 2, "error": str(e)}, store_slug=store_slug)
            conn.commit()

            metrics = analyze_kpis(conn, today, sensitivity=config.alert_sensitivity)

    if not metrics:
        raise NoData(
            f"No KPI data stored for {config.store_name or config.shop}. "
            "Run a history sync before checking."
        )

    alerts = [
        {"metric": m.name, "value": round(m.current_value, 4),
         "z_score": round(m.z_score, 2), "status": m.status.value}
        for m in metrics if m.status != AlertLevel.NORMAL
    ]
    overall = compute_overall_status(metrics, {})
    return QuickCheckResult(
        date=today,
        overall_status=overall,
        alerts=alerts,
        normal_count=sum(1 for m in metrics if m.status == AlertLevel.NORMAL),
        warnings=warnings,
    )


# ─── Report rendering ─────────────────────────────────────────────────────────

def _slug_for(config: SentinelConfig, store_slug: str | None) -> str:
    if store_slug:
        return store_slug
    if config.shop:
        return config.shop.split(".")[0]
    return "default"


def _format_kpi_line(name: str, metric, currency_symbol: str) -> str:
    pop = metric.pop_change
    pop_txt = f" ({pop:+.1%} PoP)" if pop is not None else ""
    if name == "conversion_rate":
        return f"- Conversion rate: {metric.current_period_avg or metric.current_value:.2%}{pop_txt}"
    if name == "orders":
        return f"- Orders: {int(metric.current_period_avg or metric.current_value):,}{pop_txt}"
    value = metric.current_period_avg or metric.current_value
    label = "Revenue" if name == "revenue" else "Avg order value"
    return f"- {label}: {currency_symbol}{value:,.0f}{pop_txt}"


def _build_digest(config: SentinelConfig, analysis: AnalysisResult,
                  ai_report: AIAnalystReport, store_report) -> str:
    """Compact markdown summary designed to paste into an email body."""
    cur_start, cur_end = analysis.boundaries[0], analysis.boundaries[1]
    symbol = CURRENCY_SYMBOLS.get(config.currency, "")
    label = MODE_LABELS.get(analysis.mode, "Briefing")

    lines = [
        f"**{config.store_name or config.shop} — {label}** ({cur_start} → {cur_end})",
        f"Status: **{analysis.overall_status.value.upper()}** — {analysis.status_reason}",
        "",
        _format_kpi_line("revenue", store_report.revenue, symbol),
        _format_kpi_line("orders", store_report.orders, symbol),
        _format_kpi_line("conversion_rate", store_report.conversion_rate, symbol),
        _format_kpi_line("aov", store_report.aov, symbol),
        "",
        f"**Top insight:** {ai_report.critical_insight or ai_report.primary_insight}",
    ]
    if ai_report.next_week_priorities:
        lines.append("")
        lines.append("**Priorities:**")
        lines.extend(f"{i}. {p}" for i, p in enumerate(ai_report.next_week_priorities, 1))
    if analysis.stale:
        lines.append("")
        lines.append(f"_Data through {analysis.coverage[1]} — latest fetch was incomplete._")
    return "\n".join(lines)


def render_report_html(config: SentinelConfig, ai_report: AIAnalystReport,
                       mode: str = "weekly", compare: str = "mom",
                       reference_date: str | None = None,
                       store_slug: str | None = None,
                       sales_channel_note: str = "",
                       analysis: AnalysisResult | None = None,
                       confirm_standing_notes=None) -> RenderResult:
    """Assemble and write the full HTML report; return path + email digest.

    overall_status is ALWAYS recomputed programmatically — the narrator's
    status is overwritten, never trusted (INC-001).
    """
    if analysis is None:
        analysis = analyze_store(config, mode=mode, compare=compare,
                                 reference_date=reference_date, sync=False,
                                 store_slug=store_slug)
    current_start, current_end, prior_start, prior_end = analysis.boundaries
    warnings = list(analysis.warnings)

    ai_report.overall_status = analysis.overall_status

    # Same clamp as the CLI narrator: never let the prose recommend cutting the
    # top-revenue channel or optimising the non-addressable 'direct' bucket.
    # Enforcement is SILENT: the rewrite is recorded in the audit log for the
    # operator, but never returned in warnings — guardrail meta-commentary must
    # not reach the narrator, the chat, or the client (it resurfaces as fake
    # "insights" if it does).
    guardrail_note = sanitize_campaign_action(ai_report, analysis.context)

    with get_connection(config.db_path) as conn:
        standing_notes = save_and_reconcile_insights(conn, ai_report, analysis.context, current_end)
        if standing_notes and confirm_standing_notes:
            standing_notes = confirm_standing_notes(standing_notes)

        sparklines = get_sparkline_data(conn, analysis.metrics, analysis.reference_date)
        store_report = build_store_report(
            conn, analysis.metrics, ai_report, sparklines,
            currency=config.currency, standing_notes=standing_notes,
            boundaries=analysis.boundaries, reference_date=analysis.reference_date,
        )

        # Client-requested Sales Channel Breakdown (order-attributed, monthly only).
        if mode == "monthly":
            try:
                from .fetcher import fetch_sales_by_channel
                from .report_builder import assemble_sales_channel_breakdown
                cur_ch = fetch_sales_by_channel(config, current_start, current_end)
                pri_ch = fetch_sales_by_channel(config, prior_start, prior_end)
                store_report.sales_channel_breakdown = assemble_sales_channel_breakdown(cur_ch, pri_ch)
                store_report.sales_channel_note = sales_channel_note
            except Exception as e:
                warnings.append(f"Sales channel breakdown unavailable: {e}")

        audit_details = {"period_start": current_start, "period_end": current_end,
                         "report_mode": mode}
        if guardrail_note:
            audit_details["guardrail"] = guardrail_note
        insert_audit_log(conn, mode=mode, action="report_generated",
                         details=audit_details, store_slug=store_slug)
        conn.commit()

    html = generate_store_briefing(
        store_report,
        store_name=config.store_name,
        store_url=config.shop,
        currency=config.currency,
        period_start=current_start,
        period_end=current_end,
        report_mode=mode,
        compare=compare,
    )

    out_dir = reports_dir(_slug_for(config, store_slug))
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{current_end}_{mode}.html"
    html_path.write_text(html)

    return RenderResult(
        html_path=str(html_path),
        subject_line=build_report_subject(config.store_name, analysis.overall_status, mode),
        markdown_digest=_build_digest(config, analysis, ai_report, store_report),
        email_recipients=list(config.email_recipients),
        period_start=current_start,
        period_end=current_end,
        html=html,
        email_html=generate_email_html(
            store_report, store_name=config.store_name, currency=config.currency,
            period_start=current_start, period_end=current_end,
            report_mode=mode, compare=compare,
        ),
        warnings=warnings,
    )


# ─── Store inventory / maintenance ────────────────────────────────────────────

def _store_db_status(db_path: str) -> tuple[str | None, int, float]:
    """Return (latest_data_date, days_of_history, db_size_mb) for a store DB."""
    if not os.path.exists(db_path):
        return None, 0, 0.0
    size_mb = os.path.getsize(db_path) / 1_000_000
    try:
        with get_connection(db_path) as conn:
            earliest, latest = _kpi_coverage(conn)
    except Exception:
        return None, 0, size_mb
    days = 0
    if earliest and latest:
        days = (datetime.strptime(latest, "%Y-%m-%d")
                - datetime.strptime(earliest, "%Y-%m-%d")).days + 1
    return latest, days, size_mb


def list_stores() -> list[StoreStatus]:
    """All configured stores: stores.json entries plus the env-configured default."""
    result = []
    seen_shops = set()

    try:
        with open(stores_path()) as f:
            stores = json.load(f)
    except FileNotFoundError:
        stores = {}

    for slug in sorted(stores):
        entry = stores[slug]
        shop = entry.get("shop", "")
        seen_shops.add(shop)
        latest, days, size_mb = _store_db_status(str(db_path_for(slug)))
        result.append(StoreStatus(
            slug=slug, shop=shop,
            store_name=entry.get("store_name", slug),
            currency=entry.get("currency", "USD"),
            source="stores.json",
            latest_data_date=latest, days_of_history=days, db_size_mb=round(size_mb, 1),
        ))

    env_config = get_config()
    if env_config.shop and env_config.shop not in seen_shops:
        latest, days, size_mb = _store_db_status(env_config.db_path)
        result.append(StoreStatus(
            slug=_slug_for(env_config, None), shop=env_config.shop,
            store_name=env_config.store_name, currency=env_config.currency,
            source="env",
            latest_data_date=latest, days_of_history=days, db_size_mb=round(size_mb, 1),
        ))

    return result


TOKEN_GUIDE = "docs/SHOPIFY_TOKEN_GUIDE.md"
REQUIRED_SCOPES = ("read_orders", "read_products", "read_reports",
                   "read_themes", "read_price_rules", "read_inventory")


def validate_store_credentials(config: SentinelConfig) -> dict:
    """Live-validate a token: shop identity query + a 1-day ShopifyQL probe
    (the probe catches a missing analytics/read_reports scope that the plain
    shop query would not). Returns {"name", "currencyCode", "url"}.
    """
    from .fetcher import _graphql_request, run_shopifyql

    try:
        data = _graphql_request(config, "{ shop { name currencyCode url } }")
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 401" in msg or "HTTP 403" in msg or "Invalid API key" in msg:
            raise TokenInvalid(
                f"Shopify rejected the token for {config.shop} ({msg[:120]}). "
                f"Check the token was copied fully (starts with 'shpat_' or 'shpca_') and "
                f"that the custom app is installed on this store — see {TOKEN_GUIDE}."
            ) from e
        raise TokenInvalid(f"Could not reach {config.shop}: {msg[:200]}") from e
    except Exception as e:
        raise TokenInvalid(f"Could not reach {config.shop}: {e}") from e

    if data.get("errors"):
        msgs = "; ".join(err.get("message", str(err)) for err in data["errors"])
        raise TokenInvalid(
            f"Shopify returned an error for {config.shop}: {msgs[:200]}. "
            f"Usually a missing Admin API scope — Sentinel needs: "
            f"{', '.join(REQUIRED_SCOPES)}. See {TOKEN_GUIDE}."
        )

    shop_info = data.get("data", {}).get("shop") or {}
    if not shop_info.get("name"):
        raise TokenInvalid(f"Unexpected response from {config.shop} — no shop data returned.")

    try:
        run_shopifyql(config, "FROM sales SHOW total_sales GROUP BY day SINCE -1d UNTIL today")
    except Exception as e:
        raise TokenInvalid(
            f"Token works but analytics access failed: {str(e)[:200]}. "
            f"The custom app needs the read_reports scope (ShopifyQL analytics), and the "
            f"store's plan must include Shopify analytics API access. See {TOKEN_GUIDE}."
        ) from e

    return shop_info


def setup_store(slug: str, shop: str, token: str = "", store_name: str = "",
                currency: str = "", store_description: str = "",
                email_recipients: list[str] | None = None,
                alert_sensitivity: str = "MED") -> SetupResult:
    """Validate credentials live, register the store in stores.json, init its DB.

    Token resolution: explicit argument first, else the SHOPIFY_TOKEN_{SLUG}
    env var. Env-sourced tokens are NOT written to stores.json (they stay in
    the environment); argument tokens are persisted (file is chmod 600).
    """
    slug = slug.strip().lower().replace(" ", "-")
    if not slug or not all(c.isalnum() or c in "-_" for c in slug):
        raise ValueError(f"Invalid store slug: {slug!r} (use letters, digits, hyphens)")
    shop = shop.strip().lower()
    if "." not in shop:
        shop = f"{shop}.myshopify.com"

    env_var = f"SHOPIFY_TOKEN_{slug.upper().replace('-', '_')}"
    token_source = "argument" if token else "env"
    resolved_token = token or os.environ.get(env_var, "") or os.environ.get(
        f"SHOPIFY_TOKEN_{slug.upper()}", "")
    if not resolved_token:
        raise TokenInvalid(
            f"No Admin API token for '{slug}'. Either set the {env_var} environment "
            f"variable in the Sentinel MCP config (recommended — keeps the token out of "
            f"the conversation) or pass admin_token directly. See {TOKEN_GUIDE} for how "
            f"to create the token."
        )

    probe = SentinelConfig(shop=shop, token=resolved_token)
    shop_info = validate_store_credentials(probe)

    store_name = store_name or shop_info.get("name", "")
    currency = (currency or shop_info.get("currencyCode", "USD")).upper()

    path = stores_path()
    try:
        with open(path) as f:
            stores = json.load(f)
    except FileNotFoundError:
        stores = {}

    entry = {
        "shop": shop,
        "store_name": store_name,
        "currency": currency,
        "store_description": store_description,
        "email_recipients": email_recipients or [],
        "alert_sensitivity": alert_sensitivity if alert_sensitivity in ("LOW", "MED", "HIGH") else "MED",
    }
    if token:  # only persist tokens that were explicitly handed over
        entry["token"] = token
    stores[slug] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stores, indent=2))
    os.replace(tmp, path)
    os.chmod(path, 0o600)

    init_db(str(db_path_for(slug)))

    return SetupResult(
        slug=slug, shop=shop, store_name=store_name, currency=currency,
        token_source=token_source,
        next_step=(f"Backfill history next: call sync_history(store='{slug}', "
                   f"total_days=180) repeatedly until done=true (730 for monthly YoY)."),
    )


def purge_store(config: SentinelConfig, older_than_days: int, confirm: bool = False,
                store_slug: str | None = None) -> PurgeResult:
    """Purge data older than N days (minimum 365). Dry-run unless confirm=True."""
    if older_than_days < 365:
        raise ValueError(
            f"older_than_days must be >= 365 (got {older_than_days}). "
            "This floor exists to prevent accidental data loss."
        )

    cutoff_date = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d")
    dry_run = not confirm

    with get_connection(config.db_path) as conn:
        purge_log = purge_old_data(conn, cutoff_date, dry_run=dry_run)
        tables = dict(purge_log)
        total = sum(tables.values())
        insert_audit_log(
            conn, mode="purge",
            action="purge_dry_run" if dry_run else "purge_executed",
            details={"cutoff_date": cutoff_date, "dry_run": dry_run,
                     "tables": tables, "total_rows": total},
            store_slug=store_slug,
        )
        conn.commit()
        if not dry_run:
            try:
                conn.execute("VACUUM")
            except Exception:
                pass  # rows are already purged; VACUUM is best-effort space reclaim

    return PurgeResult(cutoff_date=cutoff_date, dry_run=dry_run, tables=tables, total_rows=total)


def get_audit_log(config: SentinelConfig, mode_filter: str | None = None,
                  limit: int = 200) -> list[dict]:
    """Audit trail rows, newest first, with details JSON parsed."""
    with get_connection(config.db_path) as conn:
        rows = query_audit_log(conn, limit=limit, mode_filter=mode_filter)
    result = []
    for row in rows:
        entry = {k: row[k] for k in row.keys()}
        if entry.get("details"):
            try:
                entry["details"] = json.loads(entry["details"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(entry)
    return result
