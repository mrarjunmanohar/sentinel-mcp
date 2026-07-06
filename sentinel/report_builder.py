"""Assembles a StoreWeeklyReport from raw Sentinel data."""

from datetime import datetime, timedelta

from .models import (
    AIAnalystReport,
    ChannelPerformance,
    InventoryOps,
    KPIMetric,
    StoreWeeklyReport,
    ProductPerformance,
    ReportMetric,
    SalesChannelRow,
    SectionInsight,
)
from .anomaly import compute_pop_change
from .calculations import compute_wow_change, compute_pct_of_total, compute_inventory_turnover, compute_return_rate
from .db import query_top_products, query_channel_performance, query_kpi_range


def _to_float(value) -> float:
    """Coerce a ShopifyQL cell (str or number or None) to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def assemble_sales_channel_breakdown(
    current_rows: list[dict],
    prior_rows: list[dict],
    top_n: int = 5,
) -> list[SalesChannelRow]:
    """Build the Sales Channel Breakdown table from two ShopifyQL result sets.

    Pure (no I/O). Each input row is a dict from `FROM sales SHOW orders,
    net_items_sold, gross_sales GROUP BY sales_channel`. Returns the top-N
    channels by current gross_sales, each with its prior-period gross_sales and
    a gross-sales PoP %, followed by a single TOTAL row.

    The TOTAL row sums across ALL channels (not just the top N), matching
    ShopifyQL's WITH TOTALS semantics. A channel absent from the prior period
    has prior_gross_sales=None and gross_sales_change=None ("new").
    Empty current data → empty list (no TOTAL row).
    """
    if not current_rows:
        return []

    def _pct(cur: float, prior: float | None) -> float | None:
        if prior is None or prior == 0:
            return None
        return (cur - prior) / prior

    prior_gross = {
        (str(r.get("sales_channel") or "Unknown").strip() or "Unknown"): _to_float(r.get("gross_sales"))
        for r in prior_rows
    }

    parsed = [
        {
            "channel": str(r.get("sales_channel") or "Unknown").strip() or "Unknown",
            "orders": _to_float(r.get("orders")),
            "net_items_sold": _to_float(r.get("net_items_sold")),
            "gross_sales": _to_float(r.get("gross_sales")),
        }
        for r in current_rows
    ]
    parsed.sort(key=lambda r: r["gross_sales"], reverse=True)

    rows: list[SalesChannelRow] = []
    for r in parsed[:top_n]:
        prior = prior_gross.get(r["channel"])
        rows.append(SalesChannelRow(
            channel=r["channel"],
            orders=r["orders"],
            net_items_sold=r["net_items_sold"],
            gross_sales=r["gross_sales"],
            prior_gross_sales=prior,
            gross_sales_change=_pct(r["gross_sales"], prior),
        ))

    cur_total_gross = sum(r["gross_sales"] for r in parsed)
    prior_total_gross = sum(prior_gross.values()) if prior_gross else None
    rows.append(SalesChannelRow(
        channel="TOTAL",
        orders=sum(r["orders"] for r in parsed),
        net_items_sold=sum(r["net_items_sold"] for r in parsed),
        gross_sales=cur_total_gross,
        prior_gross_sales=prior_total_gross,
        gross_sales_change=_pct(cur_total_gross, prior_total_gross),
        is_total=True,
    ))
    return rows


def _metric_to_report_metric(
    kpi: KPIMetric,
    display_name: str,
    sparkline: list[float],
    pop_current_avg: float | None,
    pop_prior_avg: float | None,
) -> ReportMetric:
    """Convert a KPIMetric into a ReportMetric with PoP and sparkline."""
    return ReportMetric(
        name=kpi.name,
        display_name=display_name,
        current_value=kpi.current_value,
        current_period_avg=pop_current_avg,
        prior_period_avg=pop_prior_avg,
        sparkline_data=sparkline,
        z_score=kpi.z_score,
        status=kpi.status,
    )


def build_store_report(
    conn,
    metrics: list[KPIMetric],
    ai_report: AIAnalystReport,
    sparkline_data: dict[str, list[float]],
    currency: str = "USD",
    standing_notes: list[tuple[str, int, str]] | None = None,
    boundaries: tuple[str, str, str, str] | None = None,
    reference_date: str | None = None,
) -> StoreWeeklyReport:
    """Build a full StoreWeeklyReport with 5 sections.

    Enriches KPIs with PoP, queries product/channel/inventory data,
    and assembles section insights from the AI report.

    Uses canonical boundaries from date_utils — never computes its own date ranges.
    """
    if reference_date is None:
        reference_date = datetime.now().strftime("%Y-%m-%d")

    if boundaries:
        current_start, current_end, prior_start, prior_end = boundaries
    else:
        from .date_utils import week_boundaries
        current_start, current_end, prior_start, prior_end = week_boundaries(reference_date, "weekly")
        boundaries = (current_start, current_end, prior_start, prior_end)

    metrics_by_name = {m.name: m for m in metrics}

    def _enrich(metric_name: str, display_name: str) -> ReportMetric:
        kpi = metrics_by_name.get(metric_name)
        if not kpi:
            return ReportMetric(name=metric_name, display_name=display_name, current_value=0.0)
        pop_current, pop_prior = compute_pop_change(conn, metric_name, reference_date, boundaries=boundaries)
        return _metric_to_report_metric(
            kpi, display_name, sparkline_data.get(metric_name, []),
            pop_current, pop_prior,
        )

    revenue = _enrich("total_sales", "Revenue")
    orders = _enrich("orders", "Orders")
    conversion_rate = _enrich("conversion_rate", "Conversion Rate")
    aov = _enrich("average_order_value", "AOV")

    # --- Product Performance ---
    from .date_utils import days_in_period
    period_len = days_in_period(boundaries)
    # Suppress product-level WoW when period is too short for meaningful comparison
    suppress_product_wow = period_len < 3

    current_products = query_top_products(conn, current_start, current_end, limit=5)
    prior_products = query_top_products(conn, prior_start, prior_end, limit=50)
    prior_by_title = {p["product_title"]: p["revenue"] for p in prior_products}

    total_revenue = sum(p["revenue"] for p in current_products) or 1.0
    top_products = []
    for p in current_products:
        prior_rev = prior_by_title.get(p["product_title"])
        wow = None if suppress_product_wow else compute_wow_change(p["revenue"], prior_rev)
        top_products.append(ProductPerformance(
            product_title=p["product_title"],
            units_sold=p["units_sold"],
            revenue=p["revenue"],
            pct_total_revenue=compute_pct_of_total(p["revenue"], total_revenue),
            wow_change=wow,
            prior_revenue=prior_rev,
        ))

    # --- Low stock alerts ---
    low_stock_alerts = []
    inv_data = query_kpi_range(conn, "total_inventory_qty", reference_date, reference_date)
    total_inv = inv_data[0][1] if inv_data else 0
    if total_inv > 0:
        total_units_sold = sum(p["units_sold"] for p in current_products)
        if total_units_sold > 0:
            days_of_supply = (total_inv / total_units_sold) * 7
            if days_of_supply < 7:
                low_stock_alerts.append(
                    f"Inventory may run out in ~{days_of_supply:.0f} days at current sell-through rate"
                )

    # --- Channel Performance ---
    # PoP % is computed on REVENUE (labeled "Rev <mode>" in the email), and the
    # prior-period revenue is carried through for a side-by-side column.
    current_channels = query_channel_performance(conn, current_start, current_end)
    prior_channels = query_channel_performance(conn, prior_start, prior_end)
    prior_rev_by_channel = {(c["source"], c["name"]): c["revenue"] for c in prior_channels}

    channels = []
    for c in current_channels[:5]:  # Top 5 channels
        prior_rev = prior_rev_by_channel.get((c["source"], c["name"]))
        channels.append(ChannelPerformance(
            source=c["source"],
            name=c["name"],
            sessions=c["sessions"],
            revenue=c["revenue"],
            conversion_rate=c["conversion_rate"],
            wow_change=compute_wow_change(c["revenue"], prior_rev),
            prior_revenue=prior_rev,
        ))

    # --- Inventory & Operations ---
    def _latest_kpi(name: str) -> float:
        data = query_kpi_range(conn, name, current_start, current_end)
        return sum(v for _, v in data) if data else 0.0

    total_returns = _latest_kpi("return_count")
    total_orders_val = orders.current_value if orders.current_value else 0
    returning = _latest_kpi("returning_customers")
    first_time = _latest_kpi("first_time_customers")
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

    # --- Section insights from AI ---
    product_insight = None
    if ai_report.product_insight:
        product_insight = SectionInsight(
            insight=ai_report.product_insight,
            action=ai_report.product_action or "",
        )

    campaign_insight = None
    if ai_report.campaign_insight:
        campaign_insight = SectionInsight(
            insight=ai_report.campaign_insight,
            action=ai_report.campaign_action or "",
        )

    inventory_insight = None
    if ai_report.inventory_insight:
        inventory_insight = SectionInsight(
            insight=ai_report.inventory_insight,
            action=ai_report.inventory_action or "",
        )

    return StoreWeeklyReport(
        ai_report=ai_report,
        revenue=revenue,
        orders=orders,
        conversion_rate=conversion_rate,
        aov=aov,
        critical_insight=ai_report.critical_insight or ai_report.primary_insight,
        top_products=top_products,
        low_stock_alerts=low_stock_alerts,
        product_insight=product_insight,
        channels=channels,
        campaign_insight=campaign_insight,
        inventory_ops=inventory_ops,
        inventory_insight=inventory_insight,
        standing_notes=standing_notes or [],
        next_week_priorities=ai_report.next_week_priorities or [],
    )
