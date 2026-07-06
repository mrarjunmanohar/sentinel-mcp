"""Pydantic models for Shopify AI Sentinel."""

from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class AlertLevel(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class KPIMetric(BaseModel):
    name: str
    current_value: float
    baseline_mean: float
    standard_deviation: float
    z_score_override: float | None = Field(default=None, exclude=True)
    status_override: AlertLevel | None = Field(default=None, exclude=True)

    @property
    def z_score(self) -> float:
        if self.z_score_override is not None:
            return self.z_score_override
        if self.standard_deviation == 0:
            return 0.0
        return (self.current_value - self.baseline_mean) / self.standard_deviation

    @property
    def status(self) -> AlertLevel:
        if self.status_override is not None:
            return self.status_override
        score = abs(self.z_score)
        if score > 2.5:
            return AlertLevel.CRITICAL
        elif score > 1.5:
            return AlertLevel.WARNING
        return AlertLevel.NORMAL


class StoreEvent(BaseModel):
    event_type: str
    description: str
    timestamp: str


class TrafficSource(BaseModel):
    source: str
    name: str
    sessions: int
    conversion_rate: float | None = None


class ProductPerformance(BaseModel):
    """A single product's weekly performance."""
    product_title: str
    units_sold: float
    revenue: float
    pct_total_revenue: float = 0.0
    wow_change: float | None = None
    prior_revenue: float | None = None  # revenue in the prior comparison period


class ChannelPerformance(BaseModel):
    """A single channel's weekly performance."""
    source: str
    name: str
    sessions: float
    revenue: float
    conversion_rate: float
    wow_change: float | None = None  # PoP change in REVENUE (see report_builder)
    prior_revenue: float | None = None  # channel revenue in the prior period


class SalesChannelRow(BaseModel):
    """One row of the client-requested Sales Channel Breakdown table.

    Sourced from ShopifyQL `FROM sales ... GROUP BY sales_channel` — this is the
    ORDER-attributed sales channel (Online Store, POS, Shop app, etc.), distinct
    from the unreliable traffic-referrer channels in ChannelPerformance.
    """
    channel: str
    orders: float
    net_items_sold: float
    gross_sales: float
    prior_gross_sales: float | None = None
    gross_sales_change: float | None = None  # PoP change in gross_sales
    is_total: bool = False  # the WITH TOTALS summary row


class InventoryOps(BaseModel):
    """Inventory & operations metrics."""
    return_rate: float = 0.0
    inventory_turnover: float = 0.0
    active_sku_count: int = 0
    zero_sales_sku_count: int = 0
    returning_customer_pct: float = 0.0
    first_time_customers: int = 0
    returning_customers: int = 0


class SectionInsight(BaseModel):
    """An insight + action pair for a report section."""
    insight: str
    action: str = ""


class StoreContext(BaseModel):
    metrics: list[KPIMetric]
    recent_events: list[StoreEvent]
    top_traffic_sources: list[TrafficSource]
    pop_changes: dict[str, float] = Field(default_factory=dict, description="Week-over-week % changes keyed by metric name.")
    top_products: list[ProductPerformance] = Field(default_factory=list)
    channels: list[ChannelPerformance] = Field(default_factory=list)
    inventory_ops: InventoryOps | None = None


class AIAnalystReport(BaseModel):
    overall_status: AlertLevel
    primary_insight: str = Field(description="One-sentence summary of the most important finding.")
    reasoning: str = Field(description="The 'why' behind the status, linking metrics to store events.")
    recommended_action: str | None = Field(default=None, description="Specific task for the merchant.")
    confidence_score: float = Field(ge=0, le=1, description="How certain the AI is about this reasoning.")
    key_insights: list[str] = Field(default_factory=list, description="3 short business-focused sentences about what happened and what it means.")
    critical_insight: str | None = Field(default=None, description="Executive summary critical insight paragraph.")
    product_insight: str | None = Field(default=None, description="Insight about product performance.")
    product_action: str | None = Field(default=None, description="Recommended action for products.")
    campaign_insight: str | None = Field(default=None, description="Insight about campaign/channel performance.")
    campaign_action: str | None = Field(default=None, description="Recommended action for campaigns.")
    inventory_insight: str | None = Field(default=None, description="Insight about inventory & operations.")
    inventory_action: str | None = Field(default=None, description="Recommended action for inventory.")
    next_week_priorities: list[str] = Field(default_factory=list, description="3 AI-generated priorities for next week.")


class ReportMetric(BaseModel):
    """A metric enriched with PoP data and sparkline for the report."""
    name: str
    display_name: str
    current_value: float
    current_period_avg: float | None = None
    prior_period_avg: float | None = None
    sparkline_data: list[float] = []
    z_score: float = 0.0
    status: AlertLevel = AlertLevel.NORMAL

    @property
    def pop_change(self) -> float | None:
        """Week-over-week change (full completed Mon-Sun weeks)."""
        if self.current_period_avg is not None and self.prior_period_avg and self.prior_period_avg != 0:
            return (self.current_period_avg - self.prior_period_avg) / self.prior_period_avg
        return None


class FunnelStep(BaseModel):
    label: str
    sessions: int
    rate: float  # as decimal (0.18 = 18%)


class StoreWeeklyReport(BaseModel):
    """Full Store Weekly Performance Report with 5 sections."""
    ai_report: AIAnalystReport
    revenue: ReportMetric
    orders: ReportMetric
    conversion_rate: ReportMetric
    aov: ReportMetric
    critical_insight: str = ""
    top_products: list[ProductPerformance] = Field(default_factory=list)
    low_stock_alerts: list[str] = Field(default_factory=list)
    product_insight: SectionInsight | None = None
    channels: list[ChannelPerformance] = Field(default_factory=list)
    campaign_insight: SectionInsight | None = None
    inventory_ops: InventoryOps | None = None
    inventory_insight: SectionInsight | None = None
    standing_notes: list[tuple[str, int, str]] = Field(default_factory=list, description="Recurring insights: (category, consecutive_weeks, text)")
    next_week_priorities: list[str] = Field(default_factory=list)
    sales_channel_breakdown: list["SalesChannelRow"] = Field(default_factory=list, description="Client-requested order-attributed sales channel table (top 5 + TOTAL).")
    sales_channel_note: str = ""  # per-run insight note shown under the breakdown table
