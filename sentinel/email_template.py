"""Gmail/Outlook-compatible HTML email templates for Shopify AI Sentinel.

All layouts use table-based HTML with inline styles. No SVG, no flexbox,
no CSS Grid, no media queries, no external images (QuickChart sparklines
were removed with the pipeline update in commit 521b8b7).
"""

from datetime import datetime
from .models import (
    AlertLevel,
    KPIMetric,
    AIAnalystReport,
    ReportMetric,
    SectionInsight,
    StoreWeeklyReport,
)

_STATUS_EMOJI = {"normal": "🟢", "warning": "🟡", "critical": "🔴"}
_STATUS_LABEL = {"normal": "Healthy", "warning": "Attention Required", "critical": "Critical"}


def build_report_subject(store_name: str, overall_status: AlertLevel, report_mode: str = "weekly") -> str:
    """Build a consistent email subject line for briefings and drafts."""
    emoji = _STATUS_EMOJI.get(overall_status.value, "📊")
    label = _STATUS_LABEL.get(overall_status.value, "Report")
    mode_label = {
        "weekly": "Weekly",
        "rolling": "Mid-Week",
        "monthly": "Monthly",
    }.get(report_mode, "Weekly")
    return f"{emoji} {store_name} {mode_label} — {label}"


def generate_email_html(
    report: StoreWeeklyReport,
    store_name: str = "",
    currency: str = "USD",
    period_start: str = "",
    period_end: str = "",
    report_mode: str = "weekly",
    compare: str = "mom",
) -> str:
    """Compact email-body variant of the report (~10KB).

    Same content as generate_store_briefing — status, KPI/PoP table, products,
    channels, ops, insights, priorities — with terse markup. Exists because
    the MCP flow relays this HTML through the model into the email connector
    (create_draft htmlBody): the full report (~60KB) exceeds what a model can
    re-emit verbatim reliably, so the email body must stay small. The full
    report file remains the high-fidelity artifact on disk.
    """
    ai = report.ai_report
    status = ai.overall_status.value
    color = {"normal": "#10b981", "warning": "#f59e0b", "critical": "#ef4444"}[status]
    sym = CURRENCY_SYMBOLS.get(currency, currency + " ")
    is_monthly = report_mode == "monthly"
    title = {"weekly": "Weekly Performance Briefing", "rolling": "Mid-Week Update",
             "monthly": "Monthly Performance Briefing"}.get(report_mode, "Performance Briefing")
    pop_label = ("YoY" if compare == "yoy" else "MoM") if is_monthly else "WoW"
    plan_title = "Action Plan for Next Month" if is_monthly else "Action Plan for Next Week"

    def money(v):
        return f"{sym}{v:,.0f}"

    def pct(v, signed=True):
        if v is None:
            return "—"
        return f"{v:+.1%}" if signed else f"{v:.2%}"

    def pop_color(v):
        if v is None:
            return "#64748b"
        return "#10b981" if v >= 0 else "#ef4444"

    th = 'style="padding:6px 8px;text-align:left;font-size:12px;color:#64748b;border-bottom:1px solid #e2e8f0"'
    thr = th.replace("text-align:left", "text-align:right")
    td = 'style="padding:6px 8px;font-size:13px;border-bottom:1px solid #f1f5f9"'
    tdr = td.replace('font-size:13px', 'font-size:13px;text-align:right')

    def kpi_row(label, metric, fmt):
        pop = metric.pop_change
        val = metric.current_period_avg if metric.current_period_avg is not None else metric.current_value
        return (f'<tr><td {td}>{label}</td><td {tdr}><b>{fmt(val)}</b></td>'
                f'<td {tdr.replace("font-size:13px", f"font-size:13px;color:{pop_color(pop)}")}>{pct(pop)}</td></tr>')

    parts = [
        f'<div style="font-family:{_FONT};max-width:600px;margin:0 auto;color:#1e293b">',
        # Black banner text: readable on all three status colors, and still
        # visible if an email client (e.g. Gmail dark mode) strips the
        # background — white text goes invisible there.
        f'<div style="background:{color};color:#000000;padding:12px 18px;border-radius:8px">'
        f'<div style="font-size:16px;font-weight:700">{store_name} — {title}</div>'
        f'<div style="font-size:12px;opacity:.85">{period_start} → {period_end} &nbsp;·&nbsp; '
        f'Status: {status.upper()}</div></div>',
    ]

    if report.critical_insight:
        parts.append(f'<p style="font-size:14px;line-height:1.55;margin:16px 2px">{report.critical_insight}</p>')

    parts.append(
        f'<table style="border-collapse:collapse;width:100%;margin-top:4px">'
        f'<tr><th {th}>Key Metrics</th><th {thr}>Value</th><th {thr}>{pop_label}</th></tr>'
        + kpi_row("Revenue", report.revenue, money)
        + kpi_row("Orders", report.orders, lambda v: f"{int(v):,}")
        + kpi_row("Conversion Rate", report.conversion_rate, lambda v: pct(v, signed=False))
        + kpi_row("Avg Order Value", report.aov, money)
        + '</table>')

    if report.top_products:
        rows = "".join(
            f'<tr><td {td}>{p.product_title[:48]}</td><td {tdr}>{int(p.units_sold)}</td>'
            f'<td {tdr}>{money(p.revenue)}</td>'
            f'<td {tdr.replace("font-size:13px", f"font-size:13px;color:{pop_color(p.wow_change)}")}>{pct(p.wow_change)}</td></tr>'
            for p in report.top_products[:5])
        parts.append(
            f'<table style="border-collapse:collapse;width:100%;margin-top:14px">'
            f'<tr><th {th}>Top Products</th><th {thr}>Units</th><th {thr}>Revenue</th><th {thr}>{pop_label}</th></tr>{rows}</table>')
        if report.product_insight:
            parts.append(_insight_block(report.product_insight))

    if report.channels:
        rows = "".join(
            f'<tr><td {td}>{c.source}</td><td {tdr}>{int(c.sessions):,}</td>'
            f'<td {tdr}>{money(c.revenue)}</td>'
            f'<td {tdr}>{money(c.revenue / c.sessions) if c.sessions else "—"}</td></tr>'
            for c in report.channels[:5])
        parts.append(
            f'<table style="border-collapse:collapse;width:100%;margin-top:14px">'
            f'<tr><th {th}>Channels</th><th {thr}>Sessions</th><th {thr}>Revenue</th><th {thr}>Rev/Session</th></tr>{rows}</table>')
        if report.campaign_insight:
            parts.append(_insight_block(report.campaign_insight))

    if report.inventory_ops:
        ops = report.inventory_ops
        parts.append(
            f'<p style="font-size:13px;color:#64748b;margin:14px 2px 4px">'
            f'<b style="color:#1e293b">Operations:</b> return rate {ops.return_rate:.1%} · '
            f'returning customers {ops.returning_customer_pct:.0%} · '
            f'{ops.active_sku_count:,} active SKUs ({ops.zero_sales_sku_count:,} with no sales)</p>')
        if report.inventory_insight:
            parts.append(_insight_block(report.inventory_insight))

    if report.standing_notes:
        notes = "".join(
            f'<li style="margin:4px 0;font-size:13px">{text} '
            f'<span style="color:#94a3b8;font-size:11px">({weeks} wks · {cat})</span></li>'
            for cat, weeks, text in report.standing_notes[:5])
        parts.append(f'<p style="font-size:13px;margin:14px 2px 2px"><b>Standing notes</b></p>'
                     f'<ul style="margin:2px 0 0 18px;padding:0">{notes}</ul>')

    if report.next_week_priorities:
        items = "".join(f'<li style="margin:4px 0;font-size:13px">{p}</li>'
                        for p in report.next_week_priorities)
        parts.append(f'<p style="font-size:13px;margin:16px 2px 2px"><b>{plan_title}</b></p>'
                     f'<ol style="margin:2px 0 0 18px;padding:0">{items}</ol>')

    parts.append('<p style="font-size:11px;color:#94a3b8;margin-top:20px">— Sentinel</p></div>')
    return "".join(parts)


def _insight_block(si: SectionInsight) -> str:
    action = f' <b>Action:</b> {si.action}' if si.action else ""
    return (f'<p style="font-size:13px;line-height:1.5;margin:8px 2px;padding:8px 12px;'
            f'background:#f8fafc;border-left:3px solid #2563eb;border-radius:4px">'
            f'{si.insight}{action}</p>')


# Color scheme
COLORS = {
    "normal": "#10b981",
    "warning": "#f59e0b",
    "critical": "#ef4444",
    "critical_soft": "#f87171",
    "primary": "#2563eb",
    "bg": "#e2e8f0",
    "card_bg": "#ffffff",
    "text": "#1e293b",
    "text_muted": "#64748b",
    "border": "#e2e8f0",
}

KPI_DISPLAY_NAMES = {
    "total_sales": "Revenue",
    "sessions": "Sessions",
    "conversion_rate": "Conversion Rate",
    "average_order_value": "Avg Order Value",
}

CURRENCY_SYMBOLS = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}


def _admin_links(store_url: str) -> dict[str, str]:
    """Generate admin links from store URL."""
    base = f"https://{store_url}/admin"
    return {
        "total_sales": f"{base}/reports/finances_summary",
        "sessions": f"{base}/reports/sessions_over_time",
        "conversion_rate": f"{base}/reports/online_store_conversion_over_time",
        "average_order_value": f"{base}/reports/average_order_value_over_time",
        "themes": f"{base}/themes",
        "products": f"{base}/products",
        "orders": f"{base}/orders",
        "admin": base,
    }


# ---------------------------------------------------------------------------
# Status badge (text-based, no SVG)
# ---------------------------------------------------------------------------

def generate_status_badge(alert_level: AlertLevel) -> str:
    """Return emoji + text status badge (no SVG)."""
    badges = {
        AlertLevel.NORMAL: ("&#9989;", "#10b981", "NORMAL"),
        AlertLevel.WARNING: ("&#9888;&#65039;", "#f59e0b", "WARNING"),
        AlertLevel.CRITICAL: ("&#128308;", "#ef4444", "CRITICAL"),
    }
    _, color, label = badges.get(alert_level, ("", "#999", "UNKNOWN"))
    return f'<span style="color:{color};font-weight:700;font-size:12px;">{label}</span>'


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

def format_currency(value: float, currency: str = "USD") -> str:
    """Format a number with currency symbol. INR uses Indian comma notation."""
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")

    if value < 0:
        return f"-{symbol}{_format_abs_currency(abs(value), currency)}"

    return f"{symbol}{_format_abs_currency(value, currency)}"


def _format_abs_currency(value: float, currency: str) -> str:
    """Format absolute currency value."""
    int_part = int(round(value))
    s = str(int_part)

    if currency == "INR":
        if len(s) <= 3:
            return s
        last_three = s[-3:]
        rest = s[:-3]
        groups = []
        while rest:
            groups.append(rest[-2:])
            rest = rest[:-2]
        groups.reverse()
        return ",".join(groups) + "," + last_three
    else:
        return f"{int_part:,}"


def format_percent(value: float) -> str:
    """Format a decimal as percentage."""
    return f"{value * 100:.2f}%"


def _format_value(metric_name: str, value: float, currency: str = "USD") -> str:
    """Format a metric value based on its type."""
    if metric_name in ("total_sales", "net_sales", "average_order_value"):
        return format_currency(value, currency)
    elif metric_name in ("conversion_rate",):
        return format_percent(value)
    elif metric_name in ("sessions", "orders"):
        return f"{int(value):,}"
    return f"{value:.2f}"


def _z_score_arrow(z_score: float) -> str:
    """Return a directional arrow based on z-score."""
    if z_score > 0.5:
        return "&#9650;"   # ▲
    elif z_score < -0.5:
        return "&#9660;"   # ▼
    return "&#9654;"       # ▶


def _render_pop_badge(pct_change: float | None) -> str:
    """Render PoP badge: ▲ +18% green or ▼ -5% red."""
    if pct_change is None:
        return '<span style="font-size:12px;color:#94a3b8;">&#9654; N/A</span>'
    arrow = "&#9650;" if pct_change >= 0 else "&#9660;"
    color = COLORS["normal"] if pct_change >= 0 else COLORS["critical"]
    return f'<span style="font-size:12px;color:{color};font-weight:600;">{arrow} {pct_change:+.1%}</span>'


# ---------------------------------------------------------------------------
# KPI card (table-based) — used by critical alerts
# ---------------------------------------------------------------------------

def generate_kpi_card(
    metric: KPIMetric,
    store_url: str = "",
    currency: str = "USD",
) -> str:
    """Generate a table-based KPI card for email."""
    display_name = KPI_DISPLAY_NAMES.get(metric.name, metric.name)
    color = COLORS.get(metric.status.value, COLORS["primary"])
    value_str = _format_value(metric.name, metric.current_value, currency)
    baseline_str = _format_value(metric.name, metric.baseline_mean, currency)
    arrow = _z_score_arrow(metric.z_score)
    links = _admin_links(store_url)
    admin_link = links.get(metric.name, links["orders"])
    badge = generate_status_badge(metric.status)

    return f"""<table width="100%" cellpadding="0" cellspacing="0" style="background:{COLORS['card_bg']};border:1px solid {COLORS['border']};border-left:4px solid {color};border-radius:8px;margin-bottom:12px;">
  <tr>
    <td style="padding:16px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="vertical-align:top;">
            <span style="font-size:12px;color:{COLORS['text_muted']};text-transform:uppercase;letter-spacing:0.5px;">{display_name}</span><br/>
            <span style="font-size:24px;font-weight:700;color:{COLORS['text']};">{value_str}</span>
            <span style="font-size:16px;color:{color};">{arrow}</span>
          </td>
          <td style="vertical-align:top;text-align:right;width:80px;">
            <span style="display:block;text-align:right;">{badge}</span>
          </td>
        </tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid {COLORS['border']};margin-top:8px;padding-top:8px;">
        <tr>
          <td style="font-size:11px;color:{COLORS['text_muted']};padding-top:8px;">
            Baseline: {baseline_str} &nbsp;|&nbsp; Z-Score: {metric.z_score:+.2f}
          </td>
          <td style="text-align:right;padding-top:8px;">
            <a href="{admin_link}" style="color:{COLORS['primary']};text-decoration:none;font-weight:600;font-size:11px;">View in Shopify &#8594;</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


# ---------------------------------------------------------------------------
# Critical Alert (table-based)
# ---------------------------------------------------------------------------

def generate_critical_alert(
    report: AIAnalystReport,
    metric: KPIMetric,
    store_name: str = "",
    store_url: str = "",
    currency: str = "USD",
) -> str:
    """Generate HTML email for a critical alert — Gmail/Outlook compatible."""
    value_str = _format_value(metric.name, metric.current_value, currency)
    baseline_str = _format_value(metric.name, metric.baseline_mean, currency)
    display_name = KPI_DISPLAY_NAMES.get(metric.name, metric.name)
    links = _admin_links(store_url)
    admin_link = links.get(metric.name, links["orders"])

    action_html = ""
    if report.recommended_action:
        action_html = f"""<tr>
          <td style="padding:0 20px 16px 20px;">
            <table width="100%" cellpadding="16" cellspacing="0" style="background:#fef2f2;border:1px solid #ef4444;border-radius:8px;">
              <tr>
                <td>
                  <span style="font-weight:700;color:#991b1b;">&#9889; Action Required</span><br/>
                  <span style="color:#7f1d1d;font-size:14px;">{report.recommended_action}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <meta http-equiv="X-UA-Compatible" content="IE=edge"/>
  <title>Sentinel Critical Alert</title>
  <!--[if mso]>
  <style type="text/css">
    table {{border-collapse:collapse;}}
    .fallback-font {{font-family:Arial,sans-serif;}}
  </style>
  <![endif]-->
</head>
<body style="margin:0;padding:0;background:{COLORS['bg']};font-family:Arial,Helvetica,sans-serif;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">

<!-- OUTER WRAPPER -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:{COLORS['bg']};">
  <tr>
    <td align="center" style="padding:20px 10px;">

      <!-- INNER CONTAINER (600px) -->
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- ALERT HEADER -->
        <tr>
          <td style="background:{COLORS['critical']};border-radius:12px 12px 0 0;padding:24px;text-align:center;">
            <span style="font-size:32px;">&#128680;</span><br/>
            <span style="font-size:20px;font-weight:800;color:#ffffff;">
              CRITICAL: {display_name} Anomaly
            </span><br/>
            <span style="font-size:14px;color:rgba(255,255,255,0.8);">
              {store_name} &mdash; {store_url}
            </span>
          </td>
        </tr>

        <!-- METRIC COMPARISON (3 columns) -->
        <tr>
          <td style="background:{COLORS['card_bg']};border-left:1px solid {COLORS['border']};border-right:1px solid {COLORS['border']};padding:20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="border-bottom:1px solid {COLORS['border']};padding-bottom:16px;margin-bottom:16px;">
              <tr>
                <td width="33%" align="center" style="padding:8px;vertical-align:top;">
                  <span style="font-size:11px;color:{COLORS['text_muted']};text-transform:uppercase;">Current</span><br/>
                  <span style="font-size:20px;font-weight:700;color:{COLORS['critical']};">{value_str}</span>
                </td>
                <td width="33%" align="center" style="padding:8px;vertical-align:top;">
                  <span style="font-size:11px;color:{COLORS['text_muted']};text-transform:uppercase;">Baseline</span><br/>
                  <span style="font-size:20px;font-weight:700;color:{COLORS['text']};">{baseline_str}</span>
                </td>
                <td width="33%" align="center" style="padding:8px;vertical-align:top;">
                  <span style="font-size:11px;color:{COLORS['text_muted']};text-transform:uppercase;">Z-Score</span><br/>
                  <span style="font-size:20px;font-weight:700;color:{COLORS['critical']};">{metric.z_score:+.1f}&sigma;</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- INSIGHT -->
        <tr>
          <td style="padding:0 20px 16px 20px;background:{COLORS['card_bg']};border-left:1px solid {COLORS['border']};border-right:1px solid {COLORS['border']};">
            <span style="font-size:16px;font-weight:700;color:{COLORS['text']};">{report.primary_insight}</span><br/>
            <span style="font-size:14px;color:{COLORS['text_muted']};line-height:1.6;">{report.reasoning}</span>
          </td>
        </tr>

        {action_html}

        <!-- CTA BUTTON -->
        <tr>
          <td style="padding:20px;text-align:center;background:{COLORS['card_bg']};border-left:1px solid {COLORS['border']};border-right:1px solid {COLORS['border']};">
            <table cellpadding="0" cellspacing="0" align="center">
              <tr>
                <td style="background:{COLORS['primary']};border-radius:8px;">
                  <a href="{admin_link}" style="display:inline-block;padding:14px 28px;color:#ffffff;text-decoration:none;font-weight:700;font-size:14px;">
                    View in Shopify Admin &#8594;
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- FOOTER -->
        <tr>
          <td style="text-align:center;padding:20px;font-size:11px;color:{COLORS['text_muted']};background:{COLORS['card_bg']};border:1px solid {COLORS['border']};border-top:none;border-radius:0 0 12px 12px;">
            Powered by AI Sentinel &nbsp;|&nbsp; Confidence: {report.confidence_score:.0%}
          </td>
        </tr>

      </table>
      <!-- /INNER CONTAINER -->

    </td>
  </tr>
</table>
<!-- /OUTER WRAPPER -->

</body>
</html>"""





# ---------------------------------------------------------------------------
# MJML-based section builders — combined design (Inter font, card layout,
# insight boxes, status badges). Produces email-safe table HTML with MSO
# conditionals for Outlook compatibility.
# ---------------------------------------------------------------------------

_FONT = "Inter, Helvetica, Arial, sans-serif"


def _mjml_section(content: str, bg: str = "#FFFFFF", padding: str = "24px",
                  border_radius: str = "12px") -> str:
    """Wrap content in a full-width section with MSO conditionals and rounded corners."""
    radius = f"border-radius:{border_radius};" if border_radius else ""
    return f"""<!--[if mso | IE]><table align="center" border="0" cellpadding="0" cellspacing="0" role="presentation" style="width:600px;" width="600" bgcolor="{bg}"><tr><td style="line-height:0px;font-size:0px;mso-line-height-rule:exactly;"><![endif]-->
<div style="background:{bg};background-color:{bg};margin:0px auto;max-width:600px;{radius}">
  <table align="center" border="0" cellpadding="0" cellspacing="0" role="presentation" style="background:{bg};background-color:{bg};width:100%;{radius}">
    <tbody><tr><td style="direction:ltr;font-size:0px;padding:{padding};text-align:center;">
      <!--[if mso | IE]><table role="presentation" border="0" cellpadding="0" cellspacing="0"><tr><td style="vertical-align:top;width:552px;"><![endif]-->
      <div class="mj-column-per-100 mj-outlook-group-fix" style="font-size:0px;text-align:left;direction:ltr;display:inline-block;vertical-align:top;width:100%;">
        <table border="0" cellpadding="0" cellspacing="0" role="presentation" width="100%"><tbody><tr>
          <td style="vertical-align:top;padding:0;">
            <table border="0" cellpadding="0" cellspacing="0" role="presentation" width="100%"><tbody>
              {content}
            </tbody></table>
          </td>
        </tr></tbody></table>
      </div>
      <!--[if mso | IE]></td></tr></table><![endif]-->
    </td></tr></tbody>
  </table>
</div>
<!--[if mso | IE]></td></tr></table><![endif]-->"""


def _spacer(height: str = "8px") -> str:
    """Transparent gap between card sections."""
    return _mjml_section("", bg="transparent", padding=f"{height} 0", border_radius="")


def _mjml_text(text: str, size: str = "14px", color: str = "#374151",
               weight: str = "normal", line_height: str = "1.6",
               padding: str = "0", align: str = "left",
               extra_style: str = "") -> str:
    """Single text row inside a section tbody."""
    style = (f"font-family:{_FONT};font-size:{size};font-weight:{weight};"
             f"line-height:{line_height};text-align:{align};color:{color};{extra_style}")
    return f"""<tr><td align="{align}" style="font-size:0px;padding:{padding};word-break:break-word;">
  <div style="{style}">{text}</div>
</td></tr>"""


def _section_title(title: str) -> str:
    """20px bold section heading."""
    return _mjml_text(title, size="20px", weight="700", color="#111827", padding="0 0 16px 0")


def _insight_box(text: str, variant: str = "info") -> str:
    """Blue (info) or amber (warning) callout box."""
    if variant == "warning":
        bg, border = "#FFFBEB", "#F59E0B"
    else:
        bg, border = "#EFF6FF", "#3B82F6"
    return f"""<tr><td style="font-size:0px;padding:0;word-break:break-word;">
  <div style="font-family:{_FONT};font-size:15px;line-height:1.6;color:#374151;background-color:{bg};border-left:4px solid {border};padding:16px;border-radius:0 8px 8px 0;margin-top:16px;">
    {text}
  </div>
</td></tr>"""


_MONO = "Courier New, Courier, monospace"

# Pre-computed inline style strings — computed once at module load, reused per call
_TH = (
    f"padding:10px 12px 10px 0;font-family:{_FONT};font-size:11px;font-weight:700;"
    "color:#374151;text-transform:uppercase;letter-spacing:0.8px;"
    "border-bottom:2px solid #374151;vertical-align:bottom;"
)
_TD = (
    f"padding:14px 12px 14px 0;font-family:{_MONO};font-size:14px;"
    "color:#111827;border-bottom:1px solid #E5E7EB;vertical-align:middle;"
)
_TD_NAME = (
    f"padding:14px 12px 14px 0;font-family:{_FONT};font-size:14px;"
    "color:#111827;border-bottom:1px solid #E5E7EB;vertical-align:middle;"
)


def _data_table(inner_html: str) -> str:
    """Shared email-safe wrapper for all data tables."""
    return f"""<tr><td align="left" style="font-size:0px;padding:0;word-break:break-word;">
  <table cellpadding="0" cellspacing="0" width="100%" border="0"
         style="width:100%;border-collapse:collapse;">
    {inner_html}
  </table>
</td></tr>"""


def _wow_cell(wow_change: float | None, decimals: int = 0) -> tuple[str, str]:
    """Return (formatted string, color) for a WoW cell."""
    if wow_change is None:
        return "—", "#6B7280"
    color = "#16a34a" if wow_change >= 0 else "#DC2626"
    fmt = f"+{wow_change:.{decimals}%}" if wow_change >= 0 else f"{wow_change:.{decimals}%}"
    return fmt, color


def _product_table(products: list, currency: str, pop_label: str = "WoW", prior_col: str = "Prev Wk") -> str:
    """Product Performance table: Product | Units | Revenue | <prior> | % Total | PoP."""
    header = (
        f'<tr>'
        f'<td style="{_TH}width:34%;">Product</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Units</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Revenue</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;color:#9CA3AF;">{prior_col}</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">% Total</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">{pop_label}</td>'
        f'</tr>'
    )
    rows = ""
    for p in products[:5]:
        wow_str, wow_clr = _wow_cell(p.wow_change, decimals=1)
        units = int(p.units_sold) if p.units_sold is not None else 0
        prior_str = format_currency(p.prior_revenue, currency) if p.prior_revenue is not None else "—"
        rows += (
            f'<tr>'
            f'<td style="{_TD_NAME}">{p.product_title or ""}</td>'
            f'<td style="{_TD}text-align:right;">{units}</td>'
            f'<td style="{_TD}text-align:right;white-space:nowrap;">{format_currency(p.revenue or 0, currency)}</td>'
            f'<td style="{_TD}text-align:right;white-space:nowrap;color:#9CA3AF;">{prior_str}</td>'
            f'<td style="{_TD}text-align:right;">{p.pct_total_revenue:.1%}</td>'
            f'<td style="{_TD}text-align:right;color:{wow_clr};font-weight:700;">{wow_str}</td>'
            f'</tr>'
        )
    return _data_table(header + rows)


def _channel_table(channels: list, currency: str, pop_label: str = "Rev WoW", prior_col: str = "Prev Wk") -> str:
    """Campaign Performance table: Channel | Sessions | Revenue | <prior rev> | Rev PoP.

    The PoP % and the prior column are both REVENUE-based (see report_builder).
    """
    header = (
        f'<tr>'
        f'<td style="{_TH}width:34%;">Channel</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Sessions</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Revenue</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;color:#9CA3AF;">{prior_col}</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">{pop_label}</td>'
        f'</tr>'
    )
    rows = ""
    for c in channels[:5]:
        ch_name = f"{c.source}/{c.name}" if c.name and c.name not in ("unknown", c.source) else (c.source or "unknown")
        wow_str, wow_clr = _wow_cell(c.wow_change)
        sessions = int(c.sessions) if c.sessions is not None else 0
        prior_str = format_currency(c.prior_revenue, currency) if c.prior_revenue is not None else "—"
        rows += (
            f'<tr>'
            f'<td style="{_TD_NAME}">{ch_name}</td>'
            f'<td style="{_TD}text-align:right;">{sessions:,}</td>'
            f'<td style="{_TD}text-align:right;white-space:nowrap;">{format_currency(c.revenue or 0, currency)}</td>'
            f'<td style="{_TD}text-align:right;white-space:nowrap;color:#9CA3AF;">{prior_str}</td>'
            f'<td style="{_TD}text-align:right;color:{wow_clr};font-weight:700;">{wow_str}</td>'
            f'</tr>'
        )
    return _data_table(header + rows)


def _sales_channel_table(rows: list, currency: str, pop_label: str = "MoM", prior_col: str = "Prev Mo") -> str:
    """Sales Channel Breakdown: Channel | Orders | Items | Gross Sales | <prior> | PoP.

    Order-attributed sales_channel data (reliable). The final TOTAL row is
    rendered bold with a top border.
    """
    header = (
        f'<tr>'
        f'<td style="{_TH}width:30%;">Channel</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Orders</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Items</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Gross Sales</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;color:#9CA3AF;">{prior_col}</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">{pop_label}</td>'
        f'</tr>'
    )
    body = ""
    for r in rows:
        wow_str, wow_clr = _wow_cell(r.gross_sales_change, decimals=1)
        prior_str = format_currency(r.prior_gross_sales, currency) if r.prior_gross_sales is not None else "—"
        name_td = _TD_NAME if not r.is_total else _TD_NAME + "font-weight:700;border-top:2px solid #D1D5DB;"
        num_td = _TD if not r.is_total else _TD + "font-weight:700;border-top:2px solid #D1D5DB;"
        body += (
            f'<tr>'
            f'<td style="{name_td}">{r.channel}</td>'
            f'<td style="{num_td}text-align:right;">{int(r.orders):,}</td>'
            f'<td style="{num_td}text-align:right;">{int(r.net_items_sold):,}</td>'
            f'<td style="{num_td}text-align:right;white-space:nowrap;">{format_currency(r.gross_sales or 0, currency)}</td>'
            f'<td style="{num_td}text-align:right;white-space:nowrap;color:#9CA3AF;">{prior_str}</td>'
            f'<td style="{num_td}text-align:right;color:{wow_clr};font-weight:700;">{wow_str}</td>'
            f'</tr>'
        )
    return _data_table(header + body)


def _inventory_table(inv_rows: list, period_col: str = "This Week") -> str:
    """Inventory & Operations table: Metric | <period> | Target | Status."""
    status_colors = {
        "GOOD":  "#16a34a",
        "WATCH": "#92400E",
        "ALERT": "#DC2626",
        "INFO":  "#3730A3",
    }
    header = (
        f'<tr>'
        f'<td style="{_TH}width:40%;">Metric</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">{period_col}</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Target</td>'
        f'<td style="{_TH}text-align:right;white-space:nowrap;">Status</td>'
        f'</tr>'
    )
    rows = ""
    for metric, value, target, status_text in inv_rows:
        clr = status_colors.get(status_text, "#6B7280")
        rows += (
            f'<tr>'
            f'<td style="{_TD_NAME}">{metric}</td>'
            f'<td style="{_TD}text-align:right;">{value}</td>'
            f'<td style="{_TD}text-align:right;">{target}</td>'
            f'<td style="{_TD}text-align:right;color:{clr};font-weight:700;">{status_text}</td>'
            f'</tr>'
        )
    return _data_table(header + rows)


# ---------------------------------------------------------------------------
# Head / CSS
# ---------------------------------------------------------------------------

_MJML_HEAD = """<!doctype html>
<html lang="en" dir="auto" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <title>Performance Report</title>
  <!--[if !mso]><!--><meta http-equiv="X-UA-Compatible" content="IE=edge"><!--<![endif]-->
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" type="text/css">
  <style type="text/css">
    #outlook a { padding: 0; }
    body { margin: 0; padding: 0; -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }
    table, td { border-collapse: collapse; mso-table-lspace: 0pt; mso-table-rspace: 0pt; }
    img { border: 0; height: auto; line-height: 100%; outline: none; text-decoration: none; -ms-interpolation-mode: bicubic; }
    p { display: block; margin: 13px 0; }
  </style>
  <!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
  <!--[if lte mso 11]><style type="text/css">.mj-outlook-group-fix { width:100% !important; }</style><![endif]-->
  <style type="text/css">
    @media only screen and (min-width:480px) {
      .mj-column-per-100 { width: 100% !important; max-width: 100%; }
      .mj-column-per-50 { width: 50% !important; max-width: 50%; }
    }
  </style>
  <style media="screen and (min-width:480px)">
    .moz-text-html .mj-column-per-100 { width: 100% !important; max-width: 100%; }
    .moz-text-html .mj-column-per-50 { width: 50% !important; max-width: 50%; }
  </style>
  <style type="text/css">
    .standing-tag { display: inline-block; background: #E5E7EB; color: #6B7280; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; text-transform: uppercase; letter-spacing: 0.5px; margin-right: 8px; }
  </style>
</head>
<body style="word-spacing:normal;background-color:#F3F4F6;">
<div aria-roledescription="email" style="background-color:#F3F4F6;" role="article" lang="en" dir="auto">
"""

_MJML_FOOT = """</div></body></html>"""


# ---------------------------------------------------------------------------
# Main briefing generator (combined design)
# ---------------------------------------------------------------------------

def generate_store_briefing(
    report: StoreWeeklyReport,
    store_name: str = "",
    store_url: str = "",
    currency: str = "USD",
    period_start: str = "",
    period_end: str = "",
    report_mode: str = "weekly",
    compare: str = "mom",
) -> str:
    """Generate the Store Performance Report.

    Combined design: Inter font, card-based layout with rounded corners,
    2x2 metric cards, insight callout boxes, status badge pills.
    Works in Gmail, Apple Mail, and Outlook.

    report_mode ("weekly"/"rolling"/"monthly") drives period-specific copy:
    the briefing title, the action-plan heading, and PoP column labels.
    For monthly, compare ("mom"/"yoy") sets the PoP column label.
    """
    ai = report.ai_report
    admin_link = f"https://{store_url}/admin"

    # --- Period-specific copy ---
    is_monthly = report_mode == "monthly"
    briefing_title = "Monthly Performance Briefing" if is_monthly else "Weekly Performance Briefing"
    action_plan_title = "Action Plan for Next Month" if is_monthly else "Action Plan for Next Week"
    period_col = "This Month" if is_monthly else "This Week"
    if is_monthly:
        pop_label = "YoY" if compare == "yoy" else "MoM"
        prior_noun = "last year" if compare == "yoy" else "last month"
        prior_col = "Prev Yr" if compare == "yoy" else "Prev Mo"
    else:
        pop_label = "WoW"
        prior_noun = "last week"
        prior_col = "Prev Wk"
    # Channel PoP is computed on revenue (see report_builder) — label it as such.
    channel_pop_label = f"Rev {pop_label}"

    # --- Date range ---
    if period_start and period_end:
        start_dt = datetime.strptime(period_start, "%Y-%m-%d")
        end_dt = datetime.strptime(period_end, "%Y-%m-%d")
    else:
        from .date_utils import period_boundaries
        cs, ce, _, _ = period_boundaries(datetime.now().strftime("%Y-%m-%d"), report_mode)
        start_dt = datetime.strptime(cs, "%Y-%m-%d")
        end_dt = datetime.strptime(ce, "%Y-%m-%d")

    period_label = f"{start_dt.strftime('%b %-d').upper()} - {end_dt.strftime('%b %-d, %Y').upper()}"

    # --- Status mapping ---
    status_map = {
        "normal":   ("HEALTHY",            "#D1FAE5", "#065F46"),
        "warning":  ("ATTENTION REQUIRED",  "#FEE2E2", "#991B1B"),
        "critical": ("CRITICAL",            "#FEE2E2", "#991B1B"),
    }
    status_label, badge_bg, badge_color = status_map.get(
        ai.overall_status.value, ("WEEKLY UPDATE", "#E5E7EB", "#374151")
    )

    # --- Badge text (dynamic: includes revenue change for warning/critical) ---
    if ai.overall_status.value == "normal":
        badge_text = "HEALTHY"
    else:
        rev_change = report.revenue.pop_change
        if rev_change is not None:
            direction = "Up" if rev_change >= 0 else "Down"
            badge_text = f"Attention: Revenue {direction} {abs(rev_change):.1%}"
        else:
            badge_text = status_label

    # --- KPI data ---
    kpi_data = [
        ("Revenue", report.revenue),
        ("Orders", report.orders),
        ("Conv Rate", report.conversion_rate),
        ("AOV", report.aov),
    ]
    kpis = []
    for label, m in kpi_data:
        # Display the period-aggregate value so the headline matches the basis of
        # the PoP delta. For counts this equals the raw total; for conversion_rate
        # it is the period-total rate (orders/sessions), not the misleading
        # average-of-daily-rates. Falls back to current_value if PoP is absent.
        display_value = m.current_period_avg if m.current_period_avg is not None else m.current_value
        value_str = _format_value(m.name, display_value, currency)
        if m.pop_change is not None:
            arrow = "▲" if m.pop_change >= 0 else "▼"
            delta_str = f"{arrow} {abs(m.pop_change):.0%}"
            delta_color = "#059669" if m.pop_change >= 0 else "#DC2626"
        else:
            delta_str = "—"
            delta_color = "#6B7280"
        value_color = "#DC2626" if (m.pop_change is not None and m.pop_change < -0.20) else "#111827"
        # Prior-period value shown smaller beneath the delta.
        if m.prior_period_avg is not None:
            prior_str = f"vs {_format_value(m.name, m.prior_period_avg, currency)} {prior_noun}"
        else:
            prior_str = ""
        kpis.append({"label": label, "value": value_str, "delta": delta_str,
                      "delta_color": delta_color, "value_color": value_color,
                      "prior": prior_str})

    # --- Inventory rows ---
    inv_rows = []
    if report.inventory_ops:
        ops = report.inventory_ops

        def _inv_status(value: float, good: float, warn: float, lower_is_better: bool = False):
            if lower_is_better:
                if value <= good: return "GOOD"
                if value <= warn: return "WATCH"
                return "ALERT"
            else:
                if value >= good: return "GOOD"
                if value >= warn: return "WATCH"
                return "ALERT"

        inv_rows = [
            ("Return Rate", f"{ops.return_rate:.1%}", "< 5%", _inv_status(ops.return_rate, 0.05, 0.10, True)),
            ("Inventory Turnover", f"{ops.inventory_turnover:.2f}x", "> 0.5x", _inv_status(ops.inventory_turnover, 0.5, 0.2)),
            ("Active SKUs", str(ops.active_sku_count), "—", "INFO"),
            ("Zero-Sales SKUs", str(ops.zero_sales_sku_count), "< 10%",
             _inv_status(ops.zero_sales_sku_count / max(ops.active_sku_count, 1), 0.10, 0.25, True)),
            ("Returning Customers", f"{ops.returning_customer_pct:.1%}", "> 20%",
             _inv_status(ops.returning_customer_pct, 0.20, 0.10)),
        ]

    # ===== ASSEMBLE HTML =====
    parts = [_MJML_HEAD]

    # 1. HEADER — Status badge (left, table-based for Gmail) + date (right)
    header_content = f"""<tr><td style="font-size:0px;padding:0;word-break:break-word;">
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family:{_FONT};"><tbody><tr>
    <td style="text-align:left;vertical-align:middle;padding:0;">
      <table border="0" cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:separate;"><tbody><tr>
        <td style="background-color:{badge_bg};color:{badge_color};font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;font-family:{_FONT};padding:4px 10px;border-radius:4px;">{badge_text}</td>
      </tr></tbody></table>
    </td>
    <td style="text-align:right;vertical-align:middle;padding:0;">
      <span style="font-family:{_FONT};font-size:12px;color:#6B7280;font-weight:600;text-transform:uppercase;">{period_label}</span>
    </td>
  </tr></tbody></table>
</td></tr>"""
    parts.append(_mjml_section(header_content, padding="24px 24px 0 24px",
                               border_radius="12px 12px 0 0"))

    # Title area (connected to header — no top radius)
    title_content = _mjml_text(
        briefing_title,
        size="28px", weight="800", color="#111827", line_height="1.2", padding="0 0 8px 0"
    )
    title_content += _mjml_text(store_name, size="16px", color="#6B7280", padding="0")
    parts.append(_mjml_section(title_content, padding="32px 24px 24px 24px",
                               border_radius="0 0 12px 12px"))

    parts.append(_spacer())

    # 2. EXECUTIVE SUMMARY
    if report.critical_insight:
        exec_content = _section_title("Executive Summary")
        exec_content += _mjml_text(report.critical_insight, size="16px", color="#374151")
        parts.append(_mjml_section(exec_content))
        parts.append(_spacer())

    # 3. KEY METRICS — 2x2 grid of metric cards
    def _metric_card(kpi: dict) -> str:
        prior_html = ""
        if kpi.get("prior"):
            prior_html = (f'<div style="font-family:{_FONT};font-size:11px;font-weight:500;'
                          f'color:#9CA3AF;margin-top:4px;">{kpi["prior"]}</div>')
        return (f'<div style="background-color:#F9FAFB;border:1px solid #E5E7EB;'
                f'border-radius:8px;padding:16px;text-align:center;">'
                f'<div style="font-family:{_FONT};font-size:13px;text-transform:uppercase;'
                f'color:#6B7280;font-weight:600;margin-bottom:8px;letter-spacing:0.5px;">'
                f'{kpi["label"]}</div>'
                f'<div style="font-family:{_FONT};font-size:28px;font-weight:700;'
                f'color:{kpi["value_color"]};margin-bottom:8px;">{kpi["value"]}</div>'
                f'<div style="font-family:{_FONT};font-size:14px;font-weight:600;'
                f'color:{kpi["delta_color"]};">{kpi["delta"]}</div>'
                f'{prior_html}'
                f'</div>')

    metrics_content = _section_title("Key Metrics")
    metrics_content += f"""<tr><td style="font-size:0px;padding:0;word-break:break-word;">
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="table-layout:fixed;"><tbody>
    <tr>
      <td style="width:50%;padding:0 8px 16px 0;vertical-align:top;">{_metric_card(kpis[0])}</td>
      <td style="width:50%;padding:0 0 16px 8px;vertical-align:top;">{_metric_card(kpis[1])}</td>
    </tr>
    <tr>
      <td style="width:50%;padding:0 8px 0 0;vertical-align:top;">{_metric_card(kpis[2])}</td>
      <td style="width:50%;padding:0 0 0 8px;vertical-align:top;">{_metric_card(kpis[3])}</td>
    </tr>
  </tbody></table>
</td></tr>"""
    parts.append(_mjml_section(metrics_content))
    parts.append(_spacer())

    # 4. PRODUCT PERFORMANCE
    if report.top_products:
        prod_content = _section_title("Product Performance")
        prod_content += _product_table(report.top_products, currency, pop_label=pop_label, prior_col=prior_col)

        if report.low_stock_alerts:
            alerts_text = "<br/>".join(report.low_stock_alerts)
            prod_content += _mjml_text(
                f"&#9888; Low Stock: {alerts_text}",
                size="13px", color="#DC2626", weight="600", padding="12px 0 0 0"
            )

        if report.product_insight:
            box_text = f"<strong>Insight:</strong> {report.product_insight.insight}"
            if report.product_insight.action:
                box_text += f"<br/><br/><strong>Action:</strong> {report.product_insight.action}"
            prod_content += _insight_box(box_text, variant="warning")

        parts.append(_mjml_section(prod_content))
        parts.append(_spacer())

    # 5. CAMPAIGN PERFORMANCE
    if report.channels:
        camp_content = _section_title("Campaign Performance")
        camp_content += _channel_table(report.channels, currency, pop_label=channel_pop_label, prior_col=prior_col)

        if report.campaign_insight:
            box_text = f"<strong>Insight:</strong> {report.campaign_insight.insight}"
            if report.campaign_insight.action:
                box_text += f"<br/><br/><strong>Action:</strong> {report.campaign_insight.action}"
            camp_content += _insight_box(box_text, variant="warning")

        parts.append(_mjml_section(camp_content))
        parts.append(_spacer())

    # 5b. SALES CHANNEL BREAKDOWN (client-requested, order-attributed, monthly)
    if report.sales_channel_breakdown:
        sc_content = _section_title("Sales Channel Breakdown")
        sc_content += _sales_channel_table(
            report.sales_channel_breakdown, currency, pop_label=pop_label, prior_col=prior_col,
        )
        if report.sales_channel_note:
            sc_content += _insight_box(report.sales_channel_note, variant="info")
        parts.append(_mjml_section(sc_content))
        parts.append(_spacer())

    # 6. INVENTORY & OPERATIONS
    if inv_rows:
        ops_content = _section_title("Inventory &amp; Operations")
        ops_content += _inventory_table(inv_rows, period_col=period_col)

        if report.inventory_insight:
            box_text = f"<strong>Insight:</strong> {report.inventory_insight.insight}"
            if report.inventory_insight.action:
                box_text += f"<br/><br/><strong>Action:</strong> {report.inventory_insight.action}"
            ops_content += _insight_box(box_text, variant="warning")

        parts.append(_mjml_section(ops_content))
        parts.append(_spacer())

    # 7. STANDING NOTES
    if report.standing_notes:
        notes_html = ""
        for category, weeks, text in report.standing_notes:
            tag_style = (
                "display:inline-block;background:#E5E7EB;color:#6B7280;"
                "font-size:11px;font-weight:600;padding:2px 8px;border-radius:4px;"
                "text-transform:uppercase;letter-spacing:0.5px;margin-right:8px;"
            )
            notes_html += _mjml_text(
                f'<span style="{tag_style}">{category.title()} &middot; {weeks}w</span> {text}',
                size="14px", color="#6B7280", line_height="1.5", padding="0 0 12px 0",
            )
        sn_content = _section_title("Standing Notes")
        sn_content += notes_html
        parts.append(_mjml_section(sn_content))
        parts.append(_spacer())

    # 8. ACTION PLAN — numbered circles
    if report.next_week_priorities:
        prio_html = ""
        for i, p in enumerate(report.next_week_priorities[:3], 1):
            prio_html += f"""<tr><td style="font-size:0px;padding:0 0 12px 0;word-break:break-word;">
  <div style="font-family:{_FONT};font-size:14px;line-height:1.5;color:#374151;">
    <span style="display:inline-block;background-color:#111827;color:#ffffff;width:24px;height:24px;text-align:center;border-radius:12px;line-height:24px;font-size:12px;font-weight:bold;margin-right:12px;vertical-align:middle;">{i}</span>
    <span style="vertical-align:middle;font-weight:500;">{p}</span>
  </div>
</td></tr>"""
        pr_content = _section_title(action_plan_title)
        pr_content += prio_html
        parts.append(_mjml_section(pr_content))
        parts.append(_spacer("12px"))

    # CTA BUTTON — full-width, rounded
    btn_content = f"""<tr><td align="center" style="font-size:0px;padding:0;word-break:break-word;">
  <table border="0" cellpadding="0" cellspacing="0" role="presentation" width="100%" style="border-collapse:separate;line-height:100%;"><tbody><tr>
    <td align="center" bgcolor="#000000" role="presentation" style="border:none;border-radius:8px;cursor:auto;mso-padding-alt:18px 32px;background:#000000;" valign="middle">
      <a href="{admin_link}" rel="noopener noreferrer" style="display:block;background:#000000;color:#FFFFFF;font-family:{_FONT};font-size:16px;font-weight:600;line-height:120%;margin:0;text-decoration:none;padding:18px 32px;mso-padding-alt:0px;border-radius:8px;text-align:center;" target="_blank">Open Shopify Admin</a>
    </td>
  </tr></tbody></table>
</td></tr>"""
    parts.append(_mjml_section(btn_content, bg="transparent", padding="0 24px",
                               border_radius=""))

    parts.append(_spacer("16px"))

    # FOOTER — simple centered hyperlink, no section wrapper
    parts.append(f"""<div style="margin:0 auto;max-width:600px;text-align:center;padding:0 24px 24px 24px;">
  <a href="https://jaaxlabs.com" style="font-family:{_FONT};font-size:12px;color:#9CA3AF;text-decoration:none;" target="_blank">Generated by SENTINEL by JAAX Labs</a>
</div>""")

    parts.append(_MJML_FOOT)
    return "\n".join(parts)
