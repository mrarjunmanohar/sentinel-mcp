"""LLM reasoning engine for Shopify AI Sentinel (CLI/dev path only).

Calls Claude directly with the store context. The MCP server never imports
this module — there, the Claude Desktop host performs the narrative analysis.
"""

import json
import urllib.request
import urllib.error

from .config import SentinelConfig
from .db import insert_status_log
from .models import AIAnalystReport, AlertLevel, StoreContext
from .prompts import SENTINEL_SYSTEM_PROMPT, FEW_SHOT_EXAMPLES, build_user_prompt


def call_claude(config: SentinelConfig, system_prompt: str, user_prompt: str) -> dict:
    """Call Anthropic Claude API with structured JSON output."""
    few_shot_messages = []
    for example in FEW_SHOT_EXAMPLES:
        few_shot_messages.append({"role": "user", "content": json.dumps(example["input"], indent=2)})
        few_shot_messages.append({"role": "assistant", "content": json.dumps(example["output"], indent=2)})

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "system": system_prompt + "\n\nYou MUST respond with valid JSON only. No markdown, no code fences, no explanation outside the JSON.",
        "messages": [
            *few_shot_messages,
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": config.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()[:500]
        raise RuntimeError(f"Claude API HTTP {e.code}: {error_body}") from e

    text = data["content"][0]["text"]
    return json.loads(text)


def parse_llm_response(raw: dict) -> AIAnalystReport:
    """Parse and validate LLM response into AIAnalystReport."""
    # Handle recommended_action being empty string
    if raw.get("recommended_action") == "":
        raw["recommended_action"] = None

    # Handle optional string fields that may be empty
    for field in ("critical_insight", "product_insight", "product_action",
                  "campaign_insight", "campaign_action", "inventory_insight", "inventory_action"):
        if raw.get(field) == "":
            raw[field] = None

    # Clamp confidence score
    if "confidence_score" in raw:
        raw["confidence_score"] = max(0.0, min(1.0, float(raw["confidence_score"])))

    # Fallback: if key_insights missing, split reasoning into sentences
    if not raw.get("key_insights"):
        reasoning = raw.get("reasoning", "")
        sentences = [s.strip() for s in reasoning.replace(". ", ".\n").split("\n") if s.strip()]
        raw["key_insights"] = sentences[:3]

    # Ensure next_week_priorities is a list
    if not raw.get("next_week_priorities"):
        raw["next_week_priorities"] = []

    return AIAnalystReport(**raw)


def _log_dissonance(
    report: AIAnalystReport,
    llm_status_raw: AlertLevel | None,
    fixed_status: AlertLevel | None,
    store_context: StoreContext,
    db_conn=None,
    store_slug: str | None = None,
    report_mode: str | None = None,
    reference_date: str | None = None,
) -> None:
    """Log status comparison. Print warning when LLM disagrees with code."""
    if fixed_status is None or llm_status_raw is None:
        return

    dissonance = llm_status_raw != fixed_status
    if dissonance:
        print(f"   ⚠ DISSONANCE: LLM chose '{llm_status_raw.value}', "
              f"programmatic override → '{fixed_status.value}'")

    if db_conn is not None:
        try:
            z_scores = json.dumps({m.name: round(m.z_score, 3) for m in store_context.metrics})
            pop_json = json.dumps({k: round(v, 4) for k, v in store_context.pop_changes.items()})
            insert_status_log(
                db_conn,
                run_date=reference_date or "",
                programmatic_status=fixed_status.value,
                llm_status=llm_status_raw.value,
                dissonance=dissonance,
                z_scores_json=z_scores,
                pop_changes_json=pop_json,
                llm_reasoning=report.reasoning[:500] if report.reasoning else None,
                confidence_score=report.confidence_score,
                store_slug=store_slug,
                report_mode=report_mode,
            )
        except Exception as e:
            print(f"   Warning: status logging failed: {e}")


# Guardrail logic lives in guardrails.py (shared with the MCP render path);
# the underscore aliases keep this module's call sites and tests stable.
from .guardrails import (
    SAFE_CAMPAIGN_ACTION as _SAFE_CAMPAIGN_ACTION,
    sanitize_campaign_action as _sanitize_campaign_action,
    top_revenue_channel as _top_revenue_channel,
)


def analyze(
    config: SentinelConfig,
    store_context: StoreContext,
    prior_insights: list[dict] | None = None,
    fixed_status: AlertLevel | None = None,
    store_slug: str | None = None,
    report_mode: str | None = None,
    reference_date: str | None = None,
    db_conn=None,
    compare: str = "mom",
) -> AIAnalystReport:
    """Main entry point: analyze store context using Claude.

    If fixed_status is provided, overrides whatever the LLM returns and
    logs any dissonance between the LLM's choice and the programmatic verdict.
    """
    user_prompt = build_user_prompt(
        store_context,
        store_name=config.store_name,
        currency=config.currency,
        store_description=config.store_description,
        prior_insights=prior_insights,
        fixed_status=fixed_status,
        report_mode=report_mode or "weekly",
        compare=compare,
    )

    raw = None
    if config.anthropic_api_key:
        try:
            raw = call_claude(config, SENTINEL_SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            print(f"Claude analysis failed: {e}")
    else:
        print("No ANTHROPIC_API_KEY configured — skipping LLM analysis.")

    # Post-process: parse, override status, sanitize channel action, log dissonance
    if raw is not None:
        report = parse_llm_response(raw)
        llm_status_raw = report.overall_status
        if fixed_status is not None:
            report.overall_status = fixed_status
        rewrite_reason = _sanitize_campaign_action(report, store_context)
        if rewrite_reason:
            print(f"   ⚠ GUARDRAIL: {rewrite_reason} — rewrote campaign_action")
        _log_dissonance(report, llm_status_raw, fixed_status, store_context,
                        db_conn=db_conn, store_slug=store_slug,
                        report_mode=report_mode, reference_date=reference_date)
        return report

    # Claude unavailable — return safe fallback
    overall = fixed_status or AlertLevel.NORMAL
    if fixed_status is None:
        for m in store_context.metrics:
            if m.status == AlertLevel.CRITICAL:
                overall = AlertLevel.CRITICAL
                break
            elif m.status == AlertLevel.WARNING:
                overall = AlertLevel.WARNING

    return AIAnalystReport(
        overall_status=overall,
        primary_insight="LLM analysis unavailable. Review metrics manually.",
        reasoning=f"LLM analysis unavailable. {len(store_context.metrics)} metrics analyzed, "
                  f"{sum(1 for m in store_context.metrics if m.status != AlertLevel.NORMAL)} anomalies detected.",
        recommended_action="Check ANTHROPIC_API_KEY and retry analysis." if overall != AlertLevel.NORMAL else None,
        confidence_score=0.1,
        key_insights=[
            "Automated analysis is temporarily unavailable.",
            f"{len(store_context.metrics)} metrics were checked for anomalies.",
            "Review your Shopify admin dashboard for the latest numbers.",
        ],
    )
