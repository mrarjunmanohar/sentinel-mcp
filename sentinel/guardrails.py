"""Hard guardrails on narrative output, shared by both narrators.

The CLI's LLM call (reasoning.py) and the Claude-host narrative arriving via
the MCP render tool are both clamped here — the protection must not depend
on which narrator wrote the prose.
"""

import re

from .models import AIAnalystReport, StoreContext

# Channel sources that are non-addressable residual buckets, not acquisition channels.
DIRECT_SOURCES = {"direct", "(direct)", "none", "(none)", "unattributed", "unknown"}
# Verbs that mean "spend less on / turn off" a channel.
CUT_VERBS = r"(pause|paus|cut|cutting|stop|stopp|kill|halt|disable|turn\s*off|reduce|reducing|scale\s*back|decreas|deprioriti|defund|drop)"
# Verbs that mean "spend more on / lean into" a channel.
BOOST_VERBS = r"(optimi[sz]|focus|grow|growing|scale\s*up|double\s*down|prioriti|invest|shift\s+budget|reallocat|lean\s+into|ramp\s*up|boost)"

SAFE_CAMPAIGN_ACTION = (
    "Investigate channel tracking and attribution before reallocating any ad spend."
)


def top_revenue_channel(store_context: StoreContext) -> str | None:
    """Name of the highest-revenue acquisition channel (direct/residual excluded)."""
    best = None
    for c in store_context.channels or []:
        if (c.source or "").strip().lower() in DIRECT_SOURCES:
            continue
        if c.revenue is None:
            continue
        if best is None or c.revenue > best.revenue:
            best = c
    return (best.source or "").strip().lower() if best else None


def sanitize_campaign_action(report: AIAnalystReport, store_context: StoreContext) -> str | None:
    """Hard guardrail on the narrator's campaign_action.

    Rejects (and rewrites) two failure modes the prompt forbids:
      1. Recommending pausing/cutting the HIGHEST-REVENUE channel on a per-session
         efficiency ratio.
      2. Treating "direct" (a non-addressable residual bucket) as an optimisation
         target ("optimise/grow/focus on direct").

    Returns a reason string if the action was rewritten, else None. The prompt is
    the first line of defence; this is the belt-and-suspenders code clamp.
    """
    action = (report.campaign_action or "").strip()
    if not action:
        return None
    low = action.lower()

    # 2. Direct named as a boost/optimisation target.
    if re.search(BOOST_VERBS, low) and re.search(r"\bdirect\b", low):
        report.campaign_action = SAFE_CAMPAIGN_ACTION
        return "campaign_action targeted non-addressable 'direct' for optimisation"

    # 1. Cutting the top-revenue channel.
    top = top_revenue_channel(store_context)
    if top and re.search(CUT_VERBS, low):
        # Match the channel by its source token (e.g. "google", "facebook").
        token = re.split(r"[\s/_-]+", top)[0]
        if token and (token in low or top in low):
            report.campaign_action = SAFE_CAMPAIGN_ACTION
            return f"campaign_action recommended cutting top-revenue channel '{top}'"

    return None
