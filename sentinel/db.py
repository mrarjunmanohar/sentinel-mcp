"""SQLite storage layer for Shopify AI Sentinel."""

import getpass
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .models import StoreEvent

# Cached at import time — username doesn't change within a process
_OPERATOR = "unknown"
try:
    _OPERATOR = getpass.getuser()
except OSError:
    pass


def init_db(db_path: str) -> None:
    """Create database and tables if they don't exist."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kpi_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                value REAL NOT NULL,
                UNIQUE(date, metric_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                UNIQUE(event_type, description, timestamp)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kpi_date_metric
            ON kpi_snapshots(metric_name, date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_timestamp
            ON store_events(timestamp)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                product_title TEXT NOT NULL,
                net_quantity REAL NOT NULL DEFAULT 0,
                net_sales REAL NOT NULL DEFAULT 0,
                UNIQUE(date, product_title)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                referrer_source TEXT NOT NULL,
                referrer_name TEXT NOT NULL,
                sessions REAL NOT NULL DEFAULT 0,
                conversion_rate REAL NOT NULL DEFAULT 0,
                net_sales REAL NOT NULL DEFAULT 0,
                UNIQUE(date, referrer_source, referrer_name)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_date
            ON product_snapshots(date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_channel_date
            ON channel_snapshots(date)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS insight_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_date TEXT NOT NULL,
                category TEXT NOT NULL,
                insight_text TEXT NOT NULL,
                top_product_title TEXT,
                zero_sales_sku_count INTEGER,
                inventory_turnover_bucket TEXT,
                top_channel TEXT,
                consecutive_weeks INTEGER NOT NULL DEFAULT 1,
                resolved_at TEXT,
                UNIQUE(week_date, category)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operator TEXT NOT NULL,
                mode TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                store_slug TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
            ON audit_log(timestamp)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS status_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                store_slug TEXT,
                report_mode TEXT,
                programmatic_status TEXT NOT NULL,
                llm_status TEXT,
                dissonance INTEGER NOT NULL DEFAULT 0,
                z_scores_json TEXT,
                pop_changes_json TEXT,
                llm_reasoning TEXT,
                confidence_score REAL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_status_log_dissonance
            ON status_log(dissonance, run_date)
        """)
        conn.commit()


@contextmanager
def get_connection(db_path: str):
    """Context manager for database connections.

    WAL + a 30s busy timeout so a scheduled-task run and a manual session
    can overlap on the same store DB without 'database is locked' errors.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def insert_audit_log(
    conn: sqlite3.Connection,
    mode: str,
    action: str,
    details: dict | None = None,
    store_slug: str | None = None,
) -> None:
    """Write one row to the audit_log table. Caller must commit."""
    conn.execute(
        "INSERT INTO audit_log (timestamp, operator, mode, action, details, store_slug) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now().isoformat(),
            _OPERATOR,
            mode,
            action,
            json.dumps(details) if details else None,
            store_slug,
        ),
    )


def query_audit_log(
    conn: sqlite3.Connection,
    limit: int = 200,
    mode_filter: str | None = None,
) -> list[dict]:
    """Query audit_log rows, newest first. Optionally filter by mode."""
    query = ("SELECT id, timestamp, operator, mode, action, details, store_slug "
             "FROM audit_log")
    params: list = []
    if mode_filter:
        query += " WHERE mode = ?"
        params.append(mode_filter)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def insert_status_log(
    conn: sqlite3.Connection,
    run_date: str,
    programmatic_status: str,
    llm_status: str | None = None,
    dissonance: bool = False,
    z_scores_json: str | None = None,
    pop_changes_json: str | None = None,
    llm_reasoning: str | None = None,
    confidence_score: float | None = None,
    store_slug: str | None = None,
    report_mode: str | None = None,
) -> None:
    """Log a status comparison record."""
    conn.execute(
        "INSERT INTO status_log (run_date, store_slug, report_mode, programmatic_status, "
        "llm_status, dissonance, z_scores_json, pop_changes_json, llm_reasoning, "
        "confidence_score, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_date, store_slug, report_mode, programmatic_status,
         llm_status, int(dissonance), z_scores_json, pop_changes_json,
         llm_reasoning, confidence_score, datetime.now().isoformat()),
    )
    conn.commit()


def insert_kpi_snapshot(conn: sqlite3.Connection, date: str, metric_name: str, value: float) -> None:
    """Insert or replace a single KPI snapshot."""
    conn.execute(
        "INSERT OR REPLACE INTO kpi_snapshots (date, metric_name, value) VALUES (?, ?, ?)",
        (date, metric_name, value),
    )
    conn.commit()


def insert_kpi_snapshots_bulk(conn: sqlite3.Connection, records: list[tuple[str, str, float]]) -> None:
    """Bulk insert KPI snapshots. Each record is (date, metric_name, value)."""
    conn.executemany(
        "INSERT OR REPLACE INTO kpi_snapshots (date, metric_name, value) VALUES (?, ?, ?)",
        records,
    )
    conn.commit()


def query_kpi_range(
    conn: sqlite3.Connection, metric_name: str, start_date: str, end_date: str
) -> list[tuple[str, float]]:
    """Query KPI values for a date range. Returns list of (date, value)."""
    cursor = conn.execute(
        "SELECT date, value FROM kpi_snapshots WHERE metric_name = ? AND date >= ? AND date <= ? ORDER BY date ASC",
        (metric_name, start_date, end_date),
    )
    return [(row["date"], row["value"]) for row in cursor.fetchall()]


def insert_store_event(
    conn: sqlite3.Connection, event_type: str, description: str, timestamp: str
) -> None:
    """Insert a store event (ignores duplicates)."""
    conn.execute(
        "INSERT OR IGNORE INTO store_events (event_type, description, timestamp, fetched_at) VALUES (?, ?, ?, ?)",
        (event_type, description, timestamp, datetime.now().isoformat()),
    )
    conn.commit()


def query_recent_events(conn: sqlite3.Connection, days: int = 7) -> list[StoreEvent]:
    """Query store events from the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cursor = conn.execute(
        "SELECT event_type, description, timestamp FROM store_events WHERE timestamp >= ? ORDER BY timestamp DESC",
        (cutoff,),
    )
    return [
        StoreEvent(event_type=row["event_type"], description=row["description"], timestamp=row["timestamp"])
        for row in cursor.fetchall()
    ]


def upsert_product_snapshot(
    conn: sqlite3.Connection, date: str, title: str, qty: float, sales: float
) -> None:
    """Insert or replace a product snapshot."""
    conn.execute(
        "INSERT OR REPLACE INTO product_snapshots (date, product_title, net_quantity, net_sales) VALUES (?, ?, ?, ?)",
        (date, title, qty, sales),
    )


def upsert_product_snapshots_bulk(
    conn: sqlite3.Connection, records: list[tuple[str, str, float, float]]
) -> None:
    """Bulk upsert product snapshots. Each record is (date, title, qty, sales)."""
    conn.executemany(
        "INSERT OR REPLACE INTO product_snapshots (date, product_title, net_quantity, net_sales) VALUES (?, ?, ?, ?)",
        records,
    )
    conn.commit()


def upsert_channel_snapshot(
    conn: sqlite3.Connection, date: str, source: str, name: str,
    sessions: float, cr: float, sales: float,
) -> None:
    """Insert or replace a channel snapshot."""
    conn.execute(
        "INSERT OR REPLACE INTO channel_snapshots (date, referrer_source, referrer_name, sessions, conversion_rate, net_sales) VALUES (?, ?, ?, ?, ?, ?)",
        (date, source, name, sessions, cr, sales),
    )


def upsert_channel_snapshots_bulk(
    conn: sqlite3.Connection, records: list[tuple[str, str, str, float, float, float]]
) -> None:
    """Bulk upsert channel snapshots. Each record is (date, source, name, sessions, cr, sales)."""
    conn.executemany(
        "INSERT OR REPLACE INTO channel_snapshots (date, referrer_source, referrer_name, sessions, conversion_rate, net_sales) VALUES (?, ?, ?, ?, ?, ?)",
        records,
    )
    conn.commit()


def query_top_products(
    conn: sqlite3.Connection, start_date: str, end_date: str, limit: int = 5
) -> list[dict]:
    """Aggregate product performance by title over a date range, ordered by net_sales DESC."""
    cursor = conn.execute(
        "SELECT product_title, SUM(net_quantity) as units_sold, SUM(net_sales) as revenue "
        "FROM product_snapshots WHERE date >= ? AND date <= ? "
        "GROUP BY product_title ORDER BY revenue DESC LIMIT ?",
        (start_date, end_date, limit),
    )
    return [
        {"product_title": row[0], "units_sold": row[1], "revenue": row[2]}
        for row in cursor.fetchall()
    ]


def query_channel_performance(
    conn: sqlite3.Connection, start_date: str, end_date: str
) -> list[dict]:
    """Aggregate channel performance by source+name over a date range."""
    cursor = conn.execute(
        "SELECT referrer_source, referrer_name, SUM(sessions) as sessions, "
        "AVG(conversion_rate) as conversion_rate, SUM(net_sales) as revenue "
        "FROM channel_snapshots WHERE date >= ? AND date <= ? "
        "GROUP BY referrer_source, referrer_name ORDER BY sessions DESC",
        (start_date, end_date),
    )
    return [
        {
            "source": row[0], "name": row[1], "sessions": row[2],
            "conversion_rate": row[3], "revenue": row[4],
        }
        for row in cursor.fetchall()
    ]


def get_latest_date(conn: sqlite3.Connection, metric_name: str) -> str | None:
    """Get the most recent date for a metric."""
    cursor = conn.execute(
        "SELECT MAX(date) as max_date FROM kpi_snapshots WHERE metric_name = ?",
        (metric_name,),
    )
    row = cursor.fetchone()
    return row["max_date"] if row and row["max_date"] else None


def query_active_insights(conn: sqlite3.Connection) -> list[dict]:
    """Return active (unresolved) insights from insight_memory."""
    cursor = conn.execute(
        "SELECT category, insight_text, consecutive_weeks, top_product_title, "
        "zero_sales_sku_count, inventory_turnover_bucket, top_channel "
        "FROM insight_memory WHERE resolved_at IS NULL ORDER BY consecutive_weeks DESC"
    )
    return [dict(row) for row in cursor.fetchall()]


def _turnover_bucket(turnover: float) -> str:
    """Bucket inventory turnover into low/normal/high."""
    if turnover < 0.3:
        return "low"
    elif turnover > 1.0:
        return "high"
    return "normal"


def _fingerprint_matches(prior: dict, current: dict) -> bool:
    """Check if current data fingerprint matches a prior insight's fingerprint."""
    category = prior["category"]

    if category == "product":
        if prior["top_product_title"] and current.get("top_product_title"):
            return prior["top_product_title"] == current["top_product_title"]
        return False

    if category == "inventory":
        bucket_match = prior["inventory_turnover_bucket"] == current.get("inventory_turnover_bucket")
        prior_zs = prior["zero_sales_sku_count"]
        curr_zs = current.get("zero_sales_sku_count")
        if prior_zs is not None and curr_zs is not None:
            sku_match = abs(prior_zs - curr_zs) <= 2
        else:
            sku_match = True
        return bucket_match and sku_match

    if category == "campaign":
        if prior["top_channel"] and current.get("top_channel"):
            return prior["top_channel"] == current["top_channel"]
        return False

    return False


def purge_old_data(
    conn: sqlite3.Connection,
    cutoff_date: str,
    dry_run: bool = False,
) -> list[tuple[str, int]]:
    """Purge data older than cutoff_date from all tables.

    Returns a log of (table_name, rows_affected) for each table.
    If dry_run=True, counts rows but does not delete.
    Caller is responsible for audit logging, VACUUM, and commit.
    """
    # audit_log is intentionally excluded — compliance record, never purged
    tables = [
        ("kpi_snapshots", "date < ?", cutoff_date),
        ("product_snapshots", "date < ?", cutoff_date),
        ("channel_snapshots", "date < ?", cutoff_date),
        ("store_events", "timestamp < ?", cutoff_date),
        ("insight_memory", "resolved_at IS NOT NULL AND week_date < ?", cutoff_date),
        ("status_log", "run_date < ?", cutoff_date),
    ]

    purge_log: list[tuple[str, int]] = []

    for table, condition, param in tables:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}", (param,))
        count = cursor.fetchone()[0]

        if not dry_run and count > 0:
            conn.execute(f"DELETE FROM {table} WHERE {condition}", (param,))

        purge_log.append((table, count))

    return purge_log


def save_and_reconcile_insights(
    conn: sqlite3.Connection,
    ai_report,
    context,
    week_date: str,
) -> list[tuple[str, int, str]]:
    """Write this week's insights and reconcile with prior active insights.

    Returns standing notes: list of (category, consecutive_weeks, text) for insights
    that have persisted 2+ consecutive weeks.
    """
    # Build current fingerprint from context
    top_product = context.top_products[0].product_title if context.top_products else None
    top_channel = f"{context.channels[0].source}/{context.channels[0].name}" if context.channels else None
    inv_turnover = context.inventory_ops.inventory_turnover if context.inventory_ops else 0
    zero_skus = context.inventory_ops.zero_sales_sku_count if context.inventory_ops else 0

    current_fp = {
        "top_product_title": top_product,
        "top_channel": top_channel,
        "inventory_turnover_bucket": _turnover_bucket(inv_turnover),
        "zero_sales_sku_count": zero_skus,
    }

    # Map category → insight text from AI report
    category_insights = {
        "product": ai_report.product_insight,
        "campaign": ai_report.campaign_insight,
        "inventory": ai_report.inventory_insight,
    }

    active = query_active_insights(conn)
    active_by_cat = {row["category"]: row for row in active}
    standing_notes = []

    for category, insight_text in category_insights.items():
        if not insight_text:
            continue

        prior = active_by_cat.get(category)
        consecutive = prior["consecutive_weeks"] + 1 if prior and _fingerprint_matches(prior, current_fp) else 1

        # Resolve any active row for this category before inserting the new one
        conn.execute(
            "UPDATE insight_memory SET resolved_at = ? WHERE category = ? AND resolved_at IS NULL",
            (week_date, category),
        )

        # Insert new row for this week
        conn.execute(
            "INSERT OR REPLACE INTO insight_memory "
            "(week_date, category, insight_text, top_product_title, zero_sales_sku_count, "
            "inventory_turnover_bucket, top_channel, consecutive_weeks) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (week_date, category, insight_text, current_fp["top_product_title"],
             current_fp["zero_sales_sku_count"], current_fp["inventory_turnover_bucket"],
             current_fp["top_channel"], consecutive),
        )

        if consecutive >= 2:
            standing_notes.append((category, consecutive, insight_text))

    conn.commit()
    return standing_notes
