"""LLM prompts for Shopify AI Sentinel reasoning engine."""

from .models import StoreContext, KPIMetric, AlertLevel


SENTINEL_SYSTEM_PROMPT = """Role: You are the AI Sentinel Reasoning Engine — the intelligence layer inside a weekly performance reporting tool built for Shopify store owners.

Your job is to replace the time a store owner would otherwise spend staring at dashboards. Every Monday morning, they receive one report in plain language. No charts to decode. No tabs to open. Just the metrics that matter, what they mean, and what to do next.

The store you are analysing could sell anything — apparel, electronics, food, skincare, furniture, digital products. You do not know the category in advance unless it is provided in the store description. Reason from the data, not from assumptions about the product.

Input Data: You will receive a StoreContext containing KPIMetrics (with Z-Scores and alert levels), recent StoreEvents, top traffic sources, product performance, channel/campaign data, and inventory/operations metrics.

Report Sections:
1. Executive Summary — overall_status, primary_insight, critical_insight (a paragraph summarising the week in plain language)
2. Product Performance — product_insight + product_action based on sales data
3. Campaign Performance — campaign_insight + campaign_action based on channel and referrer data
4. Inventory & Operations — inventory_insight + inventory_action based on returns, SKU counts, customer mix
5. Next Week Priorities — exactly 3 specific, actionable priorities ordered by importance

Operational Protocol:

1. Identify Anomalies: Look for any metric where AlertLevel is WARNING or CRITICAL.
2. Correlation Analysis: Cross-reference anomalies with StoreEvents.
   - If CR is CRITICAL and a theme_update happened in the same window → "Technical Regression"
   - If CR dropped and a new app was installed → suspect checkout interference
3. Traffic Dilution Check: High sessions + low CR → check traffic sources before calling it a site error.
   - A surge from a single social channel is audience dilution, not a bug.
4. Product Analysis: Identify concentration risk. If one product accounts for >50% of revenue, flag dependency.
5. Channel Analysis: Compare sessions vs revenue per channel. High sessions, low revenue/session = mismatched audience or landing page.
   CHANNEL ATTRIBUTION IS UNRELIABLE — Shopify does not attribute orders to channels. Per-channel revenue and conversion are approximate. Frame channel findings as "investigate," NEVER as "reallocate spend" or "pause channel X."
   - NEVER recommend pausing, cutting, or reducing the HIGHEST-REVENUE channel based on a per-session efficiency ratio. Total revenue contribution outranks revenue/session. A channel can have lower revenue/session because it does upper-funnel discovery (acquiring new buyers) while another channel closes already-decided buyers.
   - DIRECT IS NOT A CHANNEL YOU CAN ADDRESS. Treat "direct" as non-addressable: it is a residual bucket (typed URLs, bookmarks, app opens, untagged links, dark social) and is largely RETURNING CUSTOMERS who were originally acquired by paid/organic channels. Its revenue/session looks high BECAUSE other channels did the acquisition.
     * NEVER recommend "optimise direct traffic," "focus on direct," "grow direct," or "shift budget to direct." There is no direct campaign to tune.
     * NEVER compare direct's efficiency favourably against an acquisition channel to justify cutting that acquisition channel.
     * Do not give "direct" the same weight as acquisition channels (google, meta/facebook, instagram, tiktok, youtube, etc.) when recommending actions. Exclude direct from any "where to invest / scale" recommendation.
     * Direct is only worth mentioning as context (e.g. "returning-customer demand is healthy"), not as an action target.
6. Operations Check: Flag return rate >5%, near-zero inventory turnover, or high zero-sales SKU count.
7. Plain Language Policy: Write as if you are briefing a busy founder at 8am on a Monday. No jargon. No "Based on the data provided." No hedging. Start with the finding.

Constraint Checklist (Strict):
- BREVITY IS PARAMOUNT. This report is read on a phone screen. Every sentence must earn its place.
- primary_insight: ONE sentence, under 15 words. Lead with the number.
- critical_insight: 2-3 sentences MAX. State the finding, the cause, and whether action is needed. No filler.
- Each section insight: 1 sentence. Each action: 1 sentence. No preamble, no "This suggests that...", no hedging.
- key_insights must be exactly 3 short sentences. No raw numbers — translate numbers into meaning.
- next_week_priorities must be exactly 3 items, each under 12 words. Start with a verb. No explanations.
- If overall_status is NORMAL, primary_insight must focus on the strongest positive signal.
- If overall_status is CRITICAL, recommended_action must be a specific, immediate task.
- campaign_action MUST NOT recommend pausing/cutting the top-revenue channel, and MUST NOT name "direct" as an optimisation target. If channel data is too thin or attribution too unreliable to act on, the action should be to investigate tracking/attribution, not to move spend.
- Format currency values using the store's currency (provided in the user prompt). Do not assume INR.
- Do not reference the store by name unless the store name is provided in the user prompt.

Output Schema:
{
  "overall_status": "normal" | "warning" | "critical",
  "primary_insight": "Under 15 words. Lead with the number.",
  "reasoning": "The 'why' behind the status, linking metrics to store events.",
  "recommended_action": "One specific task, or null if normal.",
  "confidence_score": 0.0 to 1.0,
  "key_insights": ["short sentence", "short sentence", "short sentence"],
  "critical_insight": "2-3 sentences max. Finding, cause, action needed.",
  "product_insight": "1 sentence. What happened with products.",
  "product_action": "1 sentence. Specific action to take.",
  "campaign_insight": "1 sentence. What happened with channels.",
  "campaign_action": "1 sentence. Specific action to take.",
  "inventory_insight": "1 sentence. What happened with operations.",
  "inventory_action": "1 sentence. Specific action to take.",
  "next_week_priorities": ["Verb-led, under 12 words", "Verb-led, under 12 words", "Verb-led, under 12 words"]
}"""


FEW_SHOT_EXAMPLES = [
    # Scenario 1: Technical regression — theme update broke checkout
    {
        "input": {
            "store_description": "sells handmade ceramic homeware",
            "currency": "GBP",
            "metrics": [
                {"name": "conversion_rate", "current_value": 0.006, "baseline_mean": 0.022, "standard_deviation": 0.003, "z_score": -5.3, "status": "critical"},
                {"name": "total_sales", "current_value": 1800, "baseline_mean": 4200, "standard_deviation": 900, "z_score": -2.7, "status": "critical"},
                {"name": "sessions", "current_value": 310, "baseline_mean": 290, "standard_deviation": 40, "z_score": 0.5, "status": "normal"},
            ],
            "recent_events": [
                {"event_type": "theme_update", "description": "Theme 'Craft' updated to v2.4", "timestamp": "2026-03-10T11:30:00"}
            ],
            "top_traffic_sources": [
                {"source": "google", "name": "organic", "sessions": 140},
                {"source": "direct", "name": "none", "sessions": 95},
                {"source": "social", "name": "instagram", "sessions": 55},
            ]
        },
        "output": {
            "overall_status": "critical",
            "primary_insight": "Conversion crashed 73% after yesterday's theme update.",
            "reasoning": "CR Z-Score of -5.3 is catastrophic. Sessions flat, traffic mix unchanged — rules out audience dilution. Theme update to 'Craft v2.4' at 11:30am is the only store event. Pattern consistent with a liquid template error blocking checkout.",
            "recommended_action": "Roll back the Craft theme immediately and test checkout on mobile and desktop.",
            "confidence_score": 0.96,
            "key_insights": [
                "Checkout is likely broken — conversion dropped to near-zero overnight.",
                "Traffic is normal, so customers arrive but can't complete purchases.",
                "Every hour this stays live is direct revenue loss."
            ],
            "critical_insight": "Conversion crashed immediately after a theme update. Sessions held steady while revenue collapsed. Roll back the theme or verify checkout before anything else.",
            "product_insight": "Low sales reflect a broken funnel, not weak demand.",
            "product_action": "Defer product decisions until checkout is restored.",
            "campaign_insight": "All channels delivering normal traffic — the problem is on-site.",
            "campaign_action": "Pause paid campaigns until checkout is fixed.",
            "inventory_insight": "Operations unaffected by the technical issue.",
            "inventory_action": "No action needed until site issue is resolved.",
            "next_week_priorities": [
                "Roll back theme and verify checkout on mobile and desktop",
                "Review lost revenue and plan a recovery promotion",
                "Create a pre-deployment checklist for theme changes"
            ]
        }
    },
    # Scenario 2: Audience dilution — viral social traffic, revenue still up
    {
        "input": {
            "store_description": "sells premium fitness supplements",
            "currency": "USD",
            "metrics": [
                {"name": "conversion_rate", "current_value": 0.009, "baseline_mean": 0.024, "standard_deviation": 0.004, "z_score": -3.75, "status": "critical"},
                {"name": "total_sales", "current_value": 18500, "baseline_mean": 14000, "standard_deviation": 2500, "z_score": 1.8, "status": "warning"},
                {"name": "sessions", "current_value": 2100, "baseline_mean": 580, "standard_deviation": 80, "z_score": 18.9, "status": "critical"},
                {"name": "average_order_value", "current_value": 87, "baseline_mean": 91, "standard_deviation": 12, "z_score": -0.33, "status": "normal"},
            ],
            "recent_events": [],
            "top_traffic_sources": [
                {"source": "social", "name": "tiktok", "sessions": 1450},
                {"source": "google", "name": "organic", "sessions": 320},
                {"source": "direct", "name": "none", "sessions": 210},
                {"source": "email", "name": "klaviyo", "sessions": 90},
            ]
        },
        "output": {
            "overall_status": "warning",
            "primary_insight": "Revenue up 32% despite TikTok diluting conversion rate.",
            "reasoning": "Sessions spiked 262% from TikTok. Core channels (~620 sessions) are at baseline. Low CR is dilution, not a site problem. Revenue up $4,500.",
            "recommended_action": "Build a TikTok-specific landing page to convert more social traffic.",
            "confidence_score": 0.91,
            "key_insights": [
                "TikTok viral traffic diluted conversion — not a site problem.",
                "Core channels (Google, email) converting at normal rates.",
                "Revenue up 32% — the viral spike is a net positive."
            ],
            "critical_insight": "TikTok drove a 3.6x traffic surge, pulling blended conversion down. Revenue up 32% and core channels stable. This is audience dilution, not a problem.",
            "product_insight": "AOV held steady — viral visitors have similar purchase intent.",
            "product_action": "Feature the most-viewed TikTok products in a follow-up video.",
            "campaign_insight": "TikTok is now the largest traffic source. Email and organic held share.",
            "campaign_action": "Retarget this week's TikTok visitors with a paid social campaign.",
            "inventory_insight": "32% revenue spike needs a stock check on top sellers.",
            "inventory_action": "Verify stock on top 3 products before social momentum continues.",
            "next_week_priorities": [
                "Build a TikTok-specific landing page with clear offer",
                "Launch retargeting for 1,400+ non-converting visitors",
                "Stock check top sellers before follow-up content"
            ]
        }
    },
    # Scenario 3: Healthy week — all KPIs normal, focus on growth signal
    {
        "input": {
            "store_description": "sells Scandinavian-style furniture and home accessories",
            "currency": "EUR",
            "metrics": [
                {"name": "conversion_rate", "current_value": 0.031, "baseline_mean": 0.028, "standard_deviation": 0.004, "z_score": 0.75, "status": "normal"},
                {"name": "total_sales", "current_value": 9800, "baseline_mean": 8900, "standard_deviation": 1400, "z_score": 0.64, "status": "normal"},
                {"name": "sessions", "current_value": 320, "baseline_mean": 300, "standard_deviation": 35, "z_score": 0.57, "status": "normal"},
                {"name": "average_order_value", "current_value": 312, "baseline_mean": 268, "standard_deviation": 45, "z_score": 0.98, "status": "normal"},
            ],
            "recent_events": [],
            "top_traffic_sources": [
                {"source": "google", "name": "organic", "sessions": 155},
                {"source": "direct", "name": "none", "sessions": 88},
                {"source": "email", "name": "klaviyo", "sessions": 52},
                {"source": "social", "name": "pinterest", "sessions": 21},
            ]
        },
        "output": {
            "overall_status": "normal",
            "primary_insight": "AOV up 16% — customers choosing higher-ticket items.",
            "reasoning": "All KPIs normal. AOV at €312 vs €268 baseline aligns with stable conversion and modest revenue lift. Google organic at 48% of sessions. No anomalies.",
            "recommended_action": None,
            "confidence_score": 0.89,
            "key_insights": [
                "Clean week — no issues to address.",
                "Customers choosing higher-priced products, lifting AOV.",
                "Google organic holding steady as top channel."
            ],
            "critical_insight": "Positive week. Revenue up 10% driven by higher AOV, not more traffic. All metrics normal.",
            "product_insight": "Customers gravitating toward higher-ticket SKUs.",
            "product_action": "Identify which products drove the AOV lift and feature them in email.",
            "campaign_insight": "Google organic delivering the most high-intent traffic.",
            "campaign_action": "Publish one SEO-optimised product page this week.",
            "inventory_insight": "Verify stock on hero products driving the AOV increase.",
            "inventory_action": "Reorder anything with fewer than 3 weeks of supply.",
            "next_week_priorities": [
                "Feature high-AOV products more prominently on site",
                "Publish one SEO content piece for organic growth",
                "Review slow-moving SKUs for a small promotion"
            ]
        }
    },
    # Scenario 4: Large PoP drop but normal z-scores — low-volume store variance
    {
        "input": {
            "store_description": "sells handmade lifestyle accessories",
            "currency": "INR",
            "fixed_overall_status": "warning",
            "metrics": [
                {"name": "total_sales", "current_value": 9200, "baseline_mean": 14500, "standard_deviation": 6800, "z_score": -0.78, "status": "normal"},
                {"name": "sessions", "current_value": 680, "baseline_mean": 720, "standard_deviation": 150, "z_score": -0.27, "status": "normal"},
                {"name": "conversion_rate", "current_value": 0.019, "baseline_mean": 0.018, "standard_deviation": 0.008, "z_score": 0.13, "status": "normal"},
                {"name": "orders", "current_value": 13, "baseline_mean": 15, "standard_deviation": 7, "z_score": -0.29, "status": "normal"},
                {"name": "average_order_value", "current_value": 708, "baseline_mean": 960, "standard_deviation": 380, "z_score": -0.66, "status": "normal"},
            ],
            "pop_changes": {"total_sales": -0.49, "sessions": -0.10, "orders": -0.32, "conversion_rate": -0.24, "average_order_value": -0.25},
            "recent_events": [],
            "top_traffic_sources": [
                {"source": "google", "name": "organic", "sessions": 380},
                {"source": "direct", "name": "none", "sessions": 180},
                {"source": "social", "name": "instagram", "sessions": 95},
            ]
        },
        "output": {
            "overall_status": "warning",
            "primary_insight": "Revenue down 49% — normal variance at 15 orders/week.",
            "reasoning": "Z-score -0.78 confirms this is within historical variance. At ~15 orders/week, losing 2 orders creates wild percentage swings. No store events or traffic anomalies.",
            "recommended_action": "Monitor next week to confirm one-off dip, not a trend.",
            "confidence_score": 0.85,
            "key_insights": [
                "49% revenue dip is noise — only 2 fewer orders than baseline.",
                "Conversion and traffic both stable.",
                "13 vs 15 orders is not statistically meaningful."
            ],
            "critical_insight": "Revenue down 49% but only 2 fewer orders than the 15/week baseline. All z-scores normal. Watch next week but no action needed.",
            "product_insight": "Product mix skewed to lower-priced items, pulling AOV down.",
            "product_action": "Check if hero SKUs were out of stock this week.",
            "campaign_insight": "Google organic steady. No channel disruptions.",
            "campaign_action": "Continue current SEO and social strategy.",
            "inventory_insight": "Low volume makes inventory metrics noisy this week.",
            "inventory_action": "Verify stock on top 3 SKUs for next week.",
            "next_week_priorities": [
                "Monitor revenue to confirm one-off dip",
                "Verify hero SKUs are in stock and visible",
                "Review Instagram content frequency"
            ]
        }
    },
]


def build_user_prompt(context: StoreContext, store_name: str = "", currency: str = "USD", store_description: str = "", prior_insights: list[dict] | None = None, fixed_status: AlertLevel | None = None, report_mode: str = "weekly", compare: str = "mom") -> str:
    """Format StoreContext as structured text for the LLM.

    report_mode / compare control the period framing (week vs month, and the
    monthly comparison basis) so the LLM describes the right reporting cadence.
    The JSON schema field names (e.g. next_week_priorities) are unchanged.
    """
    # Period framing: the noun used for the reporting window and the PoP label.
    if report_mode == "monthly":
        period_noun = "month"          # "this month", "last month"
        period_adj = "This Month"      # table headers
        if compare == "yoy":
            pop_header = "Year-over-Year Changes (last completed month vs same month last year)"
            pop_vs = "vs same month last year"
        else:
            pop_header = "Month-over-Month Changes (last completed month vs prior month)"
            pop_vs = "vs last month"
    else:
        period_noun = "week"
        period_adj = "This Week"
        pop_header = "Week-over-Week Changes (last completed week vs week before)"
        pop_vs = "vs last week"

    lines = ["# Store Performance Analysis\n"]

    if store_name:
        lines.append(f"**Store:** {store_name}")
    if store_description:
        lines.append(f"**Description:** This store {store_description}")
    lines.append(f"**Currency:** {currency}")
    if fixed_status is not None:
        lines.append(f"**Overall Status (FIXED):** {fixed_status.value}")
        lines.append("IMPORTANT: The overall_status has been determined programmatically from z-scores and PoP thresholds. "
                      "You MUST use this exact value. Do NOT override it.")
    lines.append("")

    # Metrics section
    lines.append("## KPI Metrics\n")
    for m in context.metrics:
        status_emoji = {"normal": "🟢", "warning": "🟡", "critical": "🔴"}.get(m.status.value, "⚪")
        lines.append(
            f"- {status_emoji} **{m.name}**: {m.current_value:.4f} "
            f"(mean: {m.baseline_mean:.4f}, σ: {m.standard_deviation:.4f}, "
            f"z-score: {m.z_score:.2f}, status: {m.status.value})"
        )

    # Events section
    lines.append("\n## Recent Store Events\n")
    if context.recent_events:
        for e in context.recent_events:
            lines.append(f"- [{e.event_type}] {e.description} (at {e.timestamp})")
    else:
        lines.append("- No recent store events detected.")

    # Period-over-period changes
    if context.pop_changes:
        lines.append(f"\n## {pop_header}\n")
        display_names = {
            "total_sales": "Revenue",
            "sessions": "Sessions",
            "conversion_rate": "Conversion Rate",
            "average_order_value": "AOV",
            "orders": "Orders",
            "net_sales": "Net Sales",
        }
        for metric_name, pct in context.pop_changes.items():
            name = display_names.get(metric_name, metric_name)
            direction = "up" if pct >= 0 else "down"
            lines.append(f"- **{name}**: {pct:+.1%} ({direction} {pop_vs})")
        lines.append("")
        lines.append(f"IMPORTANT: These {period_noun}-over-{period_noun} changes are what the merchant will see in the email. "
                      "Your key_insights MUST be consistent with these PoP numbers. "
                      "The overall_status has been set programmatically — explain PoP changes in context. "
                      "A large PoP drop on a low-volume store may be within normal statistical variance.")

    # Traffic section
    lines.append("\n## Top Traffic Sources\n")
    if context.top_traffic_sources:
        for t in context.top_traffic_sources:
            cr_str = f", CR: {t.conversion_rate:.4f}" if t.conversion_rate is not None else ""
            lines.append(f"- {t.source}/{t.name}: {t.sessions} sessions{cr_str}")
    else:
        lines.append("- No traffic source data available.")

    # Product performance
    if context.top_products:
        lines.append(f"\n## Top Products ({period_adj})\n")
        lines.append("| Product | Units Sold | Revenue | % of Total | PoP Change |")
        lines.append("|---------|-----------|---------|-----------|------------|")
        for p in context.top_products[:5]:
            wow_str = f"{p.wow_change:+.1%}" if p.wow_change is not None else "N/A"
            lines.append(f"| {p.product_title} | {p.units_sold:.0f} | {p.revenue:,.0f} {currency} | {p.pct_total_revenue:.1%} | {wow_str} |")

    # Channel performance
    if context.channels:
        lines.append(f"\n## Channel Performance ({period_adj})\n")
        lines.append("| Channel | Sessions | Revenue | Rev/Session | PoP Change |")
        lines.append("|---------|----------|---------|-------------|------------|")
        has_direct = False
        for c in context.channels[:5]:
            wow_str = f"{c.wow_change:+.1%}" if c.wow_change is not None else "N/A"
            rev_per_session = c.revenue / c.sessions if c.sessions > 0 else 0
            is_direct = (c.source or "").strip().lower() in ("direct", "(direct)", "none", "(none)")
            flag = " ⚠️ NON-ADDRESSABLE" if is_direct else ""
            has_direct = has_direct or is_direct
            lines.append(f"| {c.source}/{c.name}{flag} | {c.sessions:.0f} | {c.revenue:,.0f} {currency} | {rev_per_session:,.0f} {currency} | {wow_str} |")
        lines.append("")
        lines.append("ATTRIBUTION CAVEAT: Shopify does NOT attribute orders to channels — per-channel revenue, Rev/Session and conversion are approximate. "
                     "Do NOT recommend pausing or cutting the highest-revenue channel based on Rev/Session. Total revenue contribution outranks efficiency ratios. "
                     "Frame channel findings as 'investigate,' not 'reallocate spend.'")
        if has_direct:
            lines.append("DIRECT is NON-ADDRESSABLE: it is a residual bucket (typed URLs, bookmarks, untagged links, dark social) and mostly RETURNING customers acquired earlier by paid/organic. "
                         "Its high Rev/Session exists BECAUSE acquisition channels did the work. NEVER recommend 'optimise/grow/focus on direct' and never use direct's efficiency to justify cutting an acquisition channel. "
                         "Exclude direct from any 'where to invest' recommendation; mention it only as returning-customer context.")

    # Inventory & operations
    if context.inventory_ops:
        ops = context.inventory_ops
        lines.append("\n## Inventory & Operations\n")
        lines.append(f"- Return Rate: {ops.return_rate:.2%}")
        lines.append(f"- Inventory Turnover: {ops.inventory_turnover:.2f}x")
        lines.append(f"- Active SKUs: {ops.active_sku_count}")
        lines.append(f"- Zero-Sales SKUs: {ops.zero_sales_sku_count}")
        lines.append(f"- Returning Customers: {ops.returning_customers} ({ops.returning_customer_pct:.1%})")
        lines.append(f"- First-Time Customers: {ops.first_time_customers}")

    # Standing context from prior weeks
    if prior_insights:
        lines.append("\n## Standing Context (Prior Weeks)\n")
        lines.append("The following insights were flagged in previous weeks and the underlying data pattern persists:")
        for pi in prior_insights:
            lines.append(f"- [{pi['category']}] ({pi['consecutive_weeks']} weeks): \"{pi['insight_text']}\"")
        lines.append("")
        lines.append("Treat these as STANDING context. Mention briefly as a reminder if still present. "
                      f"Focus your primary analysis on what CHANGED this {period_noun}.")

    lines.append("\n---")
    lines.append("Analyse the above data and return a complete AIAnalystReport JSON object with all section fields: "
                  "critical_insight, product_insight, product_action, campaign_insight, campaign_action, "
                  "inventory_insight, inventory_action, next_week_priorities.")

    return "\n".join(lines)
