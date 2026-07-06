"""Tests for building AIAnalystReport from MCP tool arguments.

The generate_report_html tool constructs the pydantic report from Claude's
narrative fields; these tests pin that contract (minimal args work, empty
strings become None, the guardrail clamps hostile campaign actions).

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_report_args
"""

from sentinel.guardrails import SAFE_CAMPAIGN_ACTION, sanitize_campaign_action
from sentinel.models import (
    AIAnalystReport,
    AlertLevel,
    ChannelPerformance,
    StoreContext,
)


def _report_from_tool_args(primary_insight, **kwargs) -> AIAnalystReport:
    """Mirror of the construction in mcp_server.generate_report_html."""
    return AIAnalystReport(
        overall_status=AlertLevel.NORMAL,
        primary_insight=primary_insight,
        reasoning=kwargs.get("reasoning") or primary_insight,
        recommended_action=kwargs.get("recommended_action") or None,
        confidence_score=0.9,
        key_insights=kwargs.get("key_insights") or [],
        critical_insight=kwargs.get("critical_insight") or None,
        product_insight=kwargs.get("product_insight") or None,
        product_action=kwargs.get("product_action") or None,
        campaign_insight=kwargs.get("campaign_insight") or None,
        campaign_action=kwargs.get("campaign_action") or None,
        inventory_insight=kwargs.get("inventory_insight") or None,
        inventory_action=kwargs.get("inventory_action") or None,
        next_week_priorities=kwargs.get("next_week_priorities") or [],
    )


def test_minimal_args_build_valid_report():
    report = _report_from_tool_args("Sales were flat this week.")
    assert report.primary_insight == "Sales were flat this week."
    assert report.reasoning == "Sales were flat this week."  # defaults to primary
    assert report.critical_insight is None
    assert report.next_week_priorities == []


def test_empty_strings_become_none():
    report = _report_from_tool_args(
        "x", critical_insight="", product_insight="", campaign_action="",
        recommended_action="")
    assert report.critical_insight is None
    assert report.product_insight is None
    assert report.campaign_action is None
    assert report.recommended_action is None


def test_full_args_round_trip():
    report = _report_from_tool_args(
        "Revenue dipped on lower sessions.",
        reasoning="Sessions fell 12% while conversion held steady.",
        critical_insight="Traffic is the story this week.",
        key_insights=["a", "b", "c"],
        product_insight="Bestseller held share.",
        product_action="Restock the bestseller.",
        campaign_insight="Google drove most revenue.",
        campaign_action="Keep Google spend steady.",
        inventory_insight="Turnover normal.",
        inventory_action="No action needed.",
        next_week_priorities=["p1", "p2", "p3"],
    )
    assert report.key_insights == ["a", "b", "c"]
    assert report.next_week_priorities == ["p1", "p2", "p3"]
    assert report.campaign_action == "Keep Google spend steady."


def test_guardrail_clamps_claude_authored_cut_of_top_channel():
    context = StoreContext(
        metrics=[], recent_events=[], top_traffic_sources=[],
        channels=[
            ChannelPerformance(source="google", name="google", sessions=1000,
                               revenue=5000.0, conversion_rate=0.01),
            ChannelPerformance(source="facebook", name="facebook", sessions=800,
                               revenue=1200.0, conversion_rate=0.01),
        ],
    )
    report = _report_from_tool_args(
        "x", campaign_action="Pause Google ads — revenue per session is weak.")
    reason = sanitize_campaign_action(report, context)
    assert reason is not None
    assert report.campaign_action == SAFE_CAMPAIGN_ACTION


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run() else 0)
