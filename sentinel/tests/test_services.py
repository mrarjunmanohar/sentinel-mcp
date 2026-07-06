"""Tests for the service layer (services.py) against a seeded tmp SQLite DB.

Covers: analyze_store boundaries/status/payload shape, NoData errors,
render_report_html output, and the no-stdout guarantee during service calls.

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_paths
"""

import io
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sentinel import services
from sentinel.config import SentinelConfig
from sentinel.db import (
    get_connection,
    init_db,
    insert_kpi_snapshots_bulk,
    upsert_channel_snapshots_bulk,
)
from sentinel.guardrails import SAFE_CAMPAIGN_ACTION
from sentinel.models import AIAnalystReport, AlertLevel

# Wednesday. Last completed Mon-Sun week: 2026-06-15 → 2026-06-21.
REFERENCE_DATE = "2026-06-24"
SEED_START = "2026-04-06"  # a Monday, 11 full weeks before the reference week ends
SEED_END = "2026-06-21"


class _PoisonedStdout(io.TextIOBase):
    def write(self, s):
        raise AssertionError(f"stdout write during service call: {s!r}")


def _no_stdout(fn):
    """Run fn with a stdout that raises on any write."""
    original = sys.stdout
    sys.stdout = _PoisonedStdout()
    try:
        return fn()
    finally:
        sys.stdout = original


def _seed_db(db_path: str) -> None:
    init_db(db_path)
    records = []
    day = datetime.strptime(SEED_START, "%Y-%m-%d")
    end = datetime.strptime(SEED_END, "%Y-%m-%d")
    while day <= end:
        date_str = day.strftime("%Y-%m-%d")
        records.extend([
            (date_str, "total_sales", 1000.0),
            (date_str, "net_sales", 950.0),
            (date_str, "orders", 10.0),
            (date_str, "sessions", 500.0),
            (date_str, "conversion_rate", 0.02),
            (date_str, "average_order_value", 100.0),
        ])
        day += timedelta(days=1)
    channel_rows = []
    day = datetime.strptime(SEED_START, "%Y-%m-%d")
    while day <= end:
        date_str = day.strftime("%Y-%m-%d")
        channel_rows.append((date_str, "google", "google", 300.0, 0.02, 700.0))
        channel_rows.append((date_str, "direct", "direct", 150.0, 0.02, 200.0))
        day += timedelta(days=1)
    with get_connection(db_path) as conn:
        insert_kpi_snapshots_bulk(conn, records)
        upsert_channel_snapshots_bulk(conn, channel_rows)
        conn.commit()


def _make_config(tmp: str) -> SentinelConfig:
    db_path = str(Path(tmp) / "test.db")
    _seed_db(db_path)
    return SentinelConfig(
        shop="teststore.myshopify.com",
        store_name="Test Store",
        currency="USD",
        email_recipients=["owner@example.com"],
        db_path=db_path,
    )


def test_analyze_store_weekly_boundaries_and_status():
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(tmp)
        analysis = _no_stdout(lambda: services.analyze_store(
            config, mode="weekly", sync=False, reference_date=REFERENCE_DATE))
        assert analysis.boundaries == ("2026-06-15", "2026-06-21", "2026-06-08", "2026-06-14"), analysis.boundaries
        assert analysis.period_days == 7, analysis.period_days
        # Flat seeded data → zero variance → everything NORMAL.
        assert analysis.overall_status == AlertLevel.NORMAL, analysis.overall_status
        assert analysis.status_reason == "all KPIs within normal range", analysis.status_reason
        assert not analysis.stale, (analysis.coverage, analysis.boundaries)


def test_analyze_store_payload_shape():
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(tmp)
        analysis = services.analyze_store(
            config, mode="weekly", sync=False, reference_date=REFERENCE_DATE)
        payload = analysis.to_payload(config)
        assert payload["store"] == "Test Store"
        assert payload["period"]["start"] == "2026-06-15"
        assert payload["overall_status"] == "normal"
        kpi_names = {k["name"] for k in payload["kpis"]}
        assert {"total_sales", "orders", "sessions", "conversion_rate"} <= kpi_names, kpi_names
        for k in payload["kpis"]:
            assert set(k) == {"name", "current", "baseline_mean", "z_score", "status", "pop_change"}, k
        assert len(payload["top_products"]) <= 5
        assert len(payload["channels"]) <= 5
        assert len(payload["recent_store_events"]) <= 10
        assert payload["data_quality"]["latest_data_date"] == SEED_END
        assert payload["data_quality"]["stale"] is False
        # Weekly payloads must not carry a compare basis (monthly-only concept).
        assert "compare" not in payload


def test_analyze_store_no_data_raises():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "empty.db")
        init_db(db_path)
        config = SentinelConfig(shop="x.myshopify.com", db_path=db_path)
        try:
            services.analyze_store(config, mode="weekly", sync=False,
                                   reference_date=REFERENCE_DATE)
            raise AssertionError("expected NoData")
        except services.NoData:
            pass


def test_analyze_store_monthly_yoy_requires_backfill():
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(tmp)  # only ~11 weeks of 2026 data — no 2025 history
        try:
            services.analyze_store(config, mode="monthly", compare="yoy",
                                   sync=False, reference_date=REFERENCE_DATE)
            raise AssertionError("expected NoData for missing year-ago month")
        except services.NoData as e:
            assert "730" in str(e), str(e)


def test_analyze_store_rejects_unknown_mode():
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(tmp)
        try:
            services.analyze_store(config, mode="daily", sync=False,
                                   reference_date=REFERENCE_DATE)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_render_report_html_writes_report_and_digest():
    with tempfile.TemporaryDirectory() as tmp:
        original_home = os.environ.get("SENTINEL_HOME")
        os.environ["SENTINEL_HOME"] = tmp
        try:
            config = _make_config(tmp)
            ai_report = AIAnalystReport(
                overall_status=AlertLevel.CRITICAL,  # must be overridden to programmatic NORMAL
                primary_insight="Steady week with flat sales.",
                reasoning="All KPIs match their baselines.",
                confidence_score=0.9,
                key_insights=["Flat revenue", "Stable traffic", "No anomalies"],
                next_week_priorities=["Restock bestsellers", "Review ad spend"],
            )
            result = _no_stdout(lambda: services.render_report_html(
                config, ai_report, mode="weekly", reference_date=REFERENCE_DATE,
                store_slug="teststore"))

            html_path = Path(result.html_path)
            assert html_path.exists(), result.html_path
            assert html_path.parent == Path(tmp) / "reports" / "teststore", html_path
            assert html_path.name == "2026-06-21_weekly.html", html_path.name
            html = html_path.read_text()
            assert "Test Store" in html

            # INC-001: programmatic status always wins over the narrator's claim.
            assert ai_report.overall_status == AlertLevel.NORMAL
            assert "🟢" in result.subject_line, result.subject_line

            digest = result.markdown_digest
            assert "Test Store" in digest
            assert "NORMAL" in digest
            assert "Restock bestsellers" in digest
            assert result.email_recipients == ["owner@example.com"]
            assert result.period_start == "2026-06-15"
            assert result.period_end == "2026-06-21"

            # Compact email variant: small enough for a model to relay
            # verbatim into a connector draft (the ~60KB full report is not).
            email = result.email_html
            assert 0 < len(email) < 16_000, len(email)
            assert "Test Store" in email
            assert "Key Metrics" in email
            assert "Restock bestsellers" in email
            assert "/Users/" not in email  # never leak local paths
        finally:
            if original_home is None:
                os.environ.pop("SENTINEL_HOME", None)
            else:
                os.environ["SENTINEL_HOME"] = original_home


def test_guardrail_clamps_silently_in_render():
    """The campaign-action clamp must apply WITHOUT surfacing in warnings —
    guardrail meta-commentary must never reach the narrator/client. The
    rewrite is recorded in the audit log for the operator instead."""
    with tempfile.TemporaryDirectory() as tmp:
        original_home = os.environ.get("SENTINEL_HOME")
        os.environ["SENTINEL_HOME"] = tmp
        try:
            config = _make_config(tmp)
            ai_report = AIAnalystReport(
                overall_status=AlertLevel.NORMAL,
                primary_insight="Steady week.",
                reasoning="Baselines matched.",
                confidence_score=0.9,
                campaign_action="Pause Google ads — weak revenue per session.",
            )
            result = services.render_report_html(
                config, ai_report, mode="weekly", reference_date=REFERENCE_DATE,
                store_slug="teststore")
            assert ai_report.campaign_action == SAFE_CAMPAIGN_ACTION, ai_report.campaign_action
            assert not any("guardrail" in w.lower() for w in result.warnings), result.warnings
            log = services.get_audit_log(config, mode_filter="weekly")
            gen = [r for r in log if r["action"] == "report_generated"][0]
            assert "guardrail" in gen["details"], gen["details"]
        finally:
            if original_home is None:
                os.environ.pop("SENTINEL_HOME", None)
            else:
                os.environ["SENTINEL_HOME"] = original_home


def test_quick_check_no_data_raises():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "empty.db")
        init_db(db_path)
        config = SentinelConfig(shop="", db_path=db_path)  # no shop → fetch fails fast
        try:
            services.run_quick_check(config)
            raise AssertionError("expected NoData")
        except services.NoData:
            pass


def test_purge_store_enforces_floor():
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(tmp)
        try:
            services.purge_store(config, 200, confirm=True)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "365" in str(e)


def test_purge_store_dry_run_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        config = _make_config(tmp)
        result = services.purge_store(config, 365)
        assert result.dry_run is True
        # Seeded data is recent — nothing is old enough to purge.
        assert result.total_rows == 0, result.total_rows
        log = services.get_audit_log(config, mode_filter="purge")
        assert log and log[0]["action"] == "purge_dry_run", log[:1]


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
    sys.exit(1 if _run() else 0)
