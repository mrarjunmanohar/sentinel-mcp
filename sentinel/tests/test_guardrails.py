"""Tests for the post-LLM campaign_action guardrail in reasoning.py.

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_guardrails
"""

from sentinel.reasoning import _sanitize_campaign_action, _top_revenue_channel, _SAFE_CAMPAIGN_ACTION
from sentinel.models import AIAnalystReport, AlertLevel, StoreContext, ChannelPerformance


def _ctx():
    return StoreContext(
        metrics=[], recent_events=[], top_traffic_sources=[], pop_changes={},
        channels=[
            ChannelPerformance(source="google", name="google", sessions=2602, revenue=22290, conversion_rate=0.0001, wow_change=-0.14),
            ChannelPerformance(source="direct", name="direct", sessions=1050, revenue=18784, conversion_rate=0.0001, wow_change=-0.96),
        ],
    )


def _mk(action):
    return AIAnalystReport(
        overall_status=AlertLevel.NORMAL, primary_insight="p", reasoning="r",
        recommended_action=None, confidence_score=0.8, key_insights=["a", "b", "c"],
        campaign_insight="x", campaign_action=action, next_week_priorities=["x", "y", "z"],
    )


def _assert(action, should_rewrite):
    r = _mk(action)
    reason = _sanitize_campaign_action(r, _ctx())
    rewrote = reason is not None
    assert rewrote == should_rewrite, f"{action!r}: rewrote={rewrote}, expected {should_rewrite}"
    if rewrote:
        assert r.campaign_action == _SAFE_CAMPAIGN_ACTION


def test_top_revenue_channel_excludes_direct():
    # Direct has higher revenue/session but google is the top *acquisition* channel.
    assert _top_revenue_channel(_ctx()) == "google"


def test_rewrites_original_bad_output():
    _assert("Pause Google ads and focus on direct traffic optimization.", True)


def test_rewrites_cutting_top_channel():
    _assert("Pause Google ads and reallocate budget.", True)
    _assert("Reduce Google spend on low-intent keywords.", True)


def test_rewrites_direct_as_boost_target():
    _assert("Focus on direct traffic optimization.", True)
    _assert("Shift budget to direct.", True)
    _assert("Grow direct traffic.", True)


def test_allows_investigate_action():
    _assert("Review Google Search Console for low-intent keywords to exclude.", False)


def test_allows_boosting_non_top_channel():
    _assert("Scale up Instagram retargeting.", False)


def test_allows_unrelated_action():
    _assert("Investigate checkout drop-off on mobile.", False)


def test_reduce_non_channel_not_rewritten():
    # "reduce" applied to returns, not a channel → must pass through.
    _assert("Reduce return rate by improving sizing guide.", False)


def test_empty_action_noop():
    r = _mk("")
    assert _sanitize_campaign_action(r, _ctx()) is None


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
