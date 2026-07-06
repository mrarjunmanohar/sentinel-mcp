"""Data fetcher for Shopify AI Sentinel via shopifyqlQuery GraphQL API."""

import json
import logging
import socket
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from .config import SentinelConfig
from .progress import StageEvent

logger = logging.getLogger(__name__)

SHOPIFY_TIMEOUT = 15   # Shopify's own server timeout is ~10s; 15s is the safe ceiling
MAX_RETRIES = 3
BACKOFF_BASE = 2       # exponential: 2s, 4s, 8s (HTTP 429 / socket timeouts)
THROTTLE_BACKOFF = 20  # linear: 20s, 40s (in-response GraphQL throttling —
                       # Shopify's analytics rate-limit windows are ~a minute)


def _is_throttled(data: dict) -> bool:
    """True if a GraphQL response body carries a rate-limit error."""
    for err in data.get("errors") or []:
        if not isinstance(err, dict):
            continue
        if (err.get("extensions") or {}).get("code") == "THROTTLED":
            return True
        if "rate limit" in (err.get("message") or "").lower():
            return True
    return False
from .db import (
    insert_kpi_snapshots_bulk,
    insert_store_event,
    upsert_product_snapshots_bulk,
    upsert_channel_snapshots_bulk,
)


GRAPHQL_TEMPLATE = """
{
  shopifyqlQuery(query: "%s") {
    parseErrors
    tableData {
      columns {
        name
        dataType
        displayName
      }
      rows
    }
  }
}
"""

# Standard GraphQL queries for store events
THEME_QUERY = """
{
  themes(first: 10) {
    nodes {
      id
      name
      role
      updatedAt
    }
  }
}
"""

PRODUCTS_QUERY = """
{
  products(first: 25, sortKey: UPDATED_AT, reverse: true) {
    nodes {
      id
      title
      updatedAt
      status
    }
  }
}
"""

PRICE_RULES_QUERY = """
{
  priceRules(first: 25, sortKey: UPDATED_AT, reverse: true) {
    nodes {
      id
      title
      status
      updatedAt
      valueV2 {
        amount
        currencyCode
      }
    }
  }
}
"""


def _graphql_request(config: SentinelConfig, query: str) -> dict:
    """Execute a GraphQL request against the Shopify Admin API.

    Retries up to MAX_RETRIES times on socket timeouts and 429s.
    All other HTTP errors are re-raised immediately.
    """
    body = json.dumps({"query": query}).encode("utf-8")
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            config.graphql_endpoint, data=body, headers=config.headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=SHOPIFY_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # Shopify also rate-limits INSIDE 200 responses (GraphQL errors with
            # THROTTLED code or "Rate limited" messages, e.g. from shopifyqlQuery
            # during deep backfills). Treat those like a 429: back off and retry.
            if _is_throttled(data) and attempt < MAX_RETRIES - 1:
                wait = THROTTLE_BACKOFF * (attempt + 1)
                logger.warning(
                    "Shopify GraphQL throttled (attempt %d/%d) — retrying in %ds",
                    attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            return data

        except socket.timeout as exc:
            wait = BACKOFF_BASE ** attempt
            logger.warning(
                "Shopify API timeout (attempt %d/%d) — retrying in %ds",
                attempt + 1, MAX_RETRIES, wait,
            )
            last_exc = exc
            time.sleep(wait)

        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                error_body = exc.read().decode()[:500]
                raise RuntimeError(f"Shopify API HTTP {exc.code}: {error_body}") from exc
            wait = float(exc.headers.get("Retry-After") or BACKOFF_BASE ** attempt)
            logger.warning(
                "Shopify API 429 rate limited (attempt %d/%d) — retrying in %.1fs",
                attempt + 1, MAX_RETRIES, wait,
            )
            last_exc = exc
            time.sleep(wait)

    raise TimeoutError(
        f"Shopify API did not respond after {MAX_RETRIES} attempts"
    ) from last_exc


def run_shopifyql(config: SentinelConfig, shopifyql: str) -> list[dict]:
    """Execute a ShopifyQL query and return parsed rows."""
    graphql = GRAPHQL_TEMPLATE % shopifyql.replace('"', '\\"')
    data = _graphql_request(config, graphql)

    if "errors" in data:
        msgs = [e.get("message", str(e)) for e in data["errors"]]
        raise RuntimeError(f"GraphQL errors: {'; '.join(msgs)}")

    result = data.get("data", {}).get("shopifyqlQuery", {})
    parse_errors = result.get("parseErrors")
    if parse_errors:
        raise RuntimeError(f"ShopifyQL parse errors: {parse_errors}")

    table = result.get("tableData")
    if not table:
        return []

    columns = [c["name"] for c in table.get("columns", [])]
    rows = table.get("rows", [])
    return [dict(zip(columns, [row.get(c) for c in columns])) for row in rows]


def fetch_sales_by_channel(config: SentinelConfig, start: str, end: str) -> list[dict]:
    """Query order-attributed sales grouped by sales_channel for a date range.

    Returns raw rows [{sales_channel, orders, net_items_sold, gross_sales}, ...]
    ordered by gross_sales desc. NOT stored — consumed live by the monthly report
    (report_builder.assemble_sales_channel_breakdown). Uses explicit calendar
    dates so it aligns exactly to the report's month boundaries.

    sales_channel is order-attributed (Online Store, POS, Shop app, custom apps),
    distinct from the unreliable traffic-referrer channels.
    """
    query = (
        f"FROM sales SHOW orders, net_items_sold, gross_sales "
        f"GROUP BY sales_channel "
        f"SINCE {start} UNTIL {end} "
        f"ORDER BY gross_sales DESC"
    )
    return run_shopifyql(config, query)


def _date_chunks(total_days: int, chunk_size: int = 30, until_days: int = 0) -> list[tuple[int, int]]:
    """Split the window (total_days → until_days] into chunks, both counting back
    from today. Returns list of (since_days, until_days) pairs. until_days > 0
    lets historical backfills fetch only an older window without re-fetching
    recent data."""
    chunks = []
    remaining = total_days
    while remaining > until_days:
        chunk = min(remaining - until_days, chunk_size)
        chunks.append((remaining, remaining - chunk))
        remaining -= chunk
    return chunks


def fetch_sales(config: SentinelConfig, conn, since_days: int = 30, until_days: int = 0) -> int:
    """Fetch daily sales data and store in DB. Returns row count."""
    total_records = []
    for since, until in _date_chunks(since_days, until_days=until_days):
        until_clause = f"-{until}d" if until > 0 else "today"
        query = (
            f"FROM sales SHOW total_sales, net_sales, orders, average_order_value "
            f"GROUP BY day SINCE -{since}d UNTIL {until_clause} ORDER BY day ASC"
        )
        rows = run_shopifyql(config, query)
        for row in rows:
            date = row.get("day", "")
            if not date:
                continue
            date_str = date[:10]  # YYYY-MM-DD
            records = []
            for metric in ["total_sales", "net_sales", "orders", "average_order_value"]:
                val = row.get(metric)
                if val is not None:
                    try:
                        records.append((date_str, metric, float(val)))
                    except (ValueError, TypeError):
                        pass
            total_records.extend(records)

    if total_records:
        insert_kpi_snapshots_bulk(conn, total_records)
    return len(total_records)


def fetch_sessions(config: SentinelConfig, conn, since_days: int = 30, until_days: int = 0) -> int:
    """Fetch daily sessions data and store in DB."""
    total_records = []
    for since, until in _date_chunks(since_days, until_days=until_days):
        until_clause = f"-{until}d" if until > 0 else "today"
        query = (
            f"FROM sessions SHOW sessions, conversion_rate "
            f"GROUP BY day SINCE -{since}d UNTIL {until_clause} ORDER BY day ASC"
        )
        rows = run_shopifyql(config, query)
        for row in rows:
            date = row.get("day", "")
            if not date:
                continue
            date_str = date[:10]
            for metric in ["sessions", "conversion_rate"]:
                val = row.get(metric)
                if val is not None:
                    try:
                        total_records.append((date_str, metric, float(val)))
                    except (ValueError, TypeError):
                        pass

    if total_records:
        insert_kpi_snapshots_bulk(conn, total_records)
    return len(total_records)


def fetch_funnel(config: SentinelConfig, conn, since_days: int = 30, until_days: int = 0) -> int:
    """Fetch checkout funnel data and store in DB."""
    total_records = []
    for since, until in _date_chunks(since_days, until_days=until_days):
        until_clause = f"-{until}d" if until > 0 else "today"
        query = (
            f"FROM sessions SHOW sessions, sessions_with_cart_additions, "
            f"sessions_that_reached_checkout, sessions_that_completed_checkout, conversion_rate "
            f"SINCE -{since}d UNTIL {until_clause} GROUP BY day ORDER BY day ASC"
        )
        rows = run_shopifyql(config, query)
        for row in rows:
            date = row.get("day", "")
            if not date:
                continue
            date_str = date[:10]
            funnel_metrics = [
                "sessions_with_cart_additions",
                "sessions_that_reached_checkout",
                "sessions_that_completed_checkout",
            ]
            for metric in funnel_metrics:
                val = row.get(metric)
                if val is not None:
                    try:
                        total_records.append((date_str, metric, float(val)))
                    except (ValueError, TypeError):
                        pass

    if total_records:
        insert_kpi_snapshots_bulk(conn, total_records)
    return len(total_records)


def fetch_traffic_sources(config: SentinelConfig, conn, since_days: int = 30) -> int:
    """Fetch traffic source data and store in DB."""
    total_records = []
    for since, until in _date_chunks(since_days):
        until_clause = f"-{until}d" if until > 0 else "today"
        query = (
            f"FROM sessions SHOW sessions, referrer_source, referrer_name "
            f"SINCE -{since}d UNTIL {until_clause} "
            f"GROUP BY referrer_source, referrer_name ORDER BY sessions DESC"
        )
        rows = run_shopifyql(config, query)
        for row in rows:
            source = row.get("referrer_source") or "unknown"
            name = row.get("referrer_name") or "unknown"
            sessions = row.get("sessions")
            if sessions is not None:
                try:
                    metric_name = f"traffic_{source}_{name}"
                    total_records.append(("aggregate", metric_name, float(sessions)))
                except (ValueError, TypeError):
                    pass

    if total_records:
        insert_kpi_snapshots_bulk(conn, total_records)
    return len(total_records)


def fetch_theme_updates(config: SentinelConfig, conn) -> int:
    """Fetch recent theme updates and store as events."""
    data = _graphql_request(config, THEME_QUERY)
    themes = data.get("data", {}).get("themes", {}).get("nodes", [])
    count = 0
    for theme in themes:
        insert_store_event(
            conn,
            event_type="theme_update",
            description=f"Theme '{theme['name']}' (role: {theme['role']})",
            timestamp=theme.get("updatedAt", datetime.now().isoformat()),
        )
        count += 1
    return count


def fetch_product_changes(config: SentinelConfig, conn) -> int:
    """Fetch recent product changes and store as events."""
    data = _graphql_request(config, PRODUCTS_QUERY)
    products = data.get("data", {}).get("products", {}).get("nodes", [])
    count = 0
    for product in products:
        insert_store_event(
            conn,
            event_type="product_update",
            description=f"Product '{product['title']}' ({product.get('status', 'unknown')})",
            timestamp=product.get("updatedAt", datetime.now().isoformat()),
        )
        count += 1
    return count


def fetch_price_rules(config: SentinelConfig, conn) -> int:
    """Fetch recent price rule changes and store as events."""
    data = _graphql_request(config, PRICE_RULES_QUERY)
    rules = data.get("data", {}).get("priceRules", {}).get("nodes", [])
    count = 0
    for rule in rules:
        value = rule.get("valueV2", {})
        desc = f"Price rule '{rule.get('title', 'unnamed')}'"
        if value:
            desc += f" ({value.get('amount', '?')} {value.get('currencyCode', '')})"
        insert_store_event(
            conn,
            event_type="price_rule_change",
            description=desc,
            timestamp=rule.get("updatedAt", datetime.now().isoformat()),
        )
        count += 1
    return count


ORDERS_QUERY_TEMPLATE = """
{
  orders(first: 250, sortKey: CREATED_AT, reverse: true, query: "created_at:>=%s created_at:<=%s") {
    edges {
      node {
        createdAt
        lineItems(first: 50) {
          edges {
            node {
              title
              quantity
              originalTotalSet {
                shopMoney {
                  amount
                }
              }
              discountedTotalSet {
                shopMoney {
                  amount
                }
              }
            }
          }
        }
      }
      cursor
    }
    pageInfo {
      hasNextPage
    }
  }
}
"""


def fetch_product_performance(config: SentinelConfig, conn, since_days: int = 30, until_days: int = 0) -> int:
    """Fetch product-level sales from Orders GraphQL API line items.

    Stores one row per (date, product_title) so that weekly date-range
    queries in report_builder can compute WoW comparisons.
    """
    start = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    end = (datetime.now() - timedelta(days=until_days)).strftime("%Y-%m-%d")

    # Aggregate units and revenue per (date, product_title)
    product_daily: dict[tuple[str, str], dict] = {}  # (date, title) -> {qty, revenue}
    cursor = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        # Inject pagination into the query template
        gql = ORDERS_QUERY_TEMPLATE.replace(
            "first: 250,",
            f"first: 250{after_clause},",
        )
        try:
            data = _graphql_request(config, gql % (start, end))
        except RuntimeError as e:
            logger.warning("Product performance (orders) query failed: %s", e)
            break

        edges = data.get("data", {}).get("orders", {}).get("edges", [])
        if not edges:
            break

        for edge in edges:
            node = edge.get("node", {})
            order_date = (node.get("createdAt") or "")[:10]  # YYYY-MM-DD
            if not order_date:
                continue
            cursor = edge.get("cursor")
            line_edges = node.get("lineItems", {}).get("edges", [])
            for li_edge in line_edges:
                li = li_edge.get("node", {})
                title = li.get("title", "")
                if not title:
                    continue
                qty = li.get("quantity", 0) or 0
                revenue = float(
                    (li.get("discountedTotalSet") or {}).get("shopMoney", {}).get("amount", 0) or 0
                )
                key = (order_date, title)
                if key not in product_daily:
                    product_daily[key] = {"qty": 0, "revenue": 0.0}
                product_daily[key]["qty"] += qty
                product_daily[key]["revenue"] += revenue

        has_next = data.get("data", {}).get("orders", {}).get("pageInfo", {}).get("hasNextPage", False)
        if not has_next:
            break

    records = [
        (date, title, vals["qty"], vals["revenue"])
        for (date, title), vals in product_daily.items()
    ]
    if records:
        upsert_product_snapshots_bulk(conn, records)
    return len(records)


def fetch_channel_data(config: SentinelConfig, conn, since_days: int = 30, until_days: int = 0) -> int:
    """Fetch daily channel performance by joining sessions + sales via ShopifyQL."""
    total_records = []

    for since, until in _date_chunks(since_days, until_days=until_days):
        until_clause = f"-{until}d" if until > 0 else "today"

        query = (
            f"FROM sessions, sales SHOW sessions, total_sales, conversion_rate "
            f"GROUP BY referring_channel, day "
            f"SINCE -{since}d UNTIL {until_clause} "
            f"ORDER BY day ASC"
        )
        try:
            rows = run_shopifyql(config, query)
        except RuntimeError as e:
            logger.warning("Channel data query failed: %s", e)
            continue

        for row in rows:
            channel = row.get("referring_channel") or "unknown"
            day = row.get("day", "")
            if not day:
                continue
            date_str = day[:10]
            sessions = float(row.get("sessions", 0) or 0)
            cr = float(row.get("conversion_rate", 0) or 0)
            sales = float(row.get("total_sales", 0) or 0)
            total_records.append((date_str, channel, channel, sessions, cr, sales))

    if total_records:
        upsert_channel_snapshots_bulk(conn, total_records)
    return len(total_records)


INVENTORY_QUERY_TEMPLATE = """
{
  products(first: 250%s) {
    edges {
      node {
        title
        totalInventory
      }
      cursor
    }
    pageInfo {
      hasNextPage
    }
  }
}
"""


def fetch_inventory_levels(config: SentinelConfig, conn) -> int:
    """Fetch total inventory via GraphQL products query with pagination."""
    total_inventory = 0
    product_count = 0
    cursor = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query = INVENTORY_QUERY_TEMPLATE % after_clause
        try:
            data = _graphql_request(config, query)
        except RuntimeError as e:
            logger.warning("Inventory fetch failed: %s", e)
            break

        edges = data.get("data", {}).get("products", {}).get("edges", [])
        if not edges:
            break

        for edge in edges:
            node = edge.get("node", {})
            inv = node.get("totalInventory", 0) or 0
            total_inventory += inv
            product_count += 1
            cursor = edge.get("cursor")

        has_next = data.get("data", {}).get("products", {}).get("pageInfo", {}).get("hasNextPage", False)
        if not has_next:
            break

    if product_count > 0:
        today = datetime.now().strftime("%Y-%m-%d")
        insert_kpi_snapshots_bulk(conn, [(today, "total_inventory_qty", float(total_inventory))])

    return product_count


PRODUCTS_COUNT_QUERY = """
{
  productsCount(query: "status:active") {
    count
  }
}
"""


def fetch_active_sku_count(config: SentinelConfig, conn) -> int:
    """Fetch count of active products."""
    try:
        data = _graphql_request(config, PRODUCTS_COUNT_QUERY)
        count = data.get("data", {}).get("productsCount", {}).get("count", 0)
        today = datetime.now().strftime("%Y-%m-%d")
        insert_kpi_snapshots_bulk(conn, [(today, "active_sku_count", float(count))])
        return count
    except RuntimeError as e:
        logger.warning("Active SKU count fetch failed: %s", e)
        return 0


def fetch_returns(config: SentinelConfig, conn, since_days: int = 30, until_days: int = 0) -> int:
    """Fetch return counts via GraphQL Admin API.

    Uses Order.returnStatus (requires read_orders scope) to count orders
    with returns per day. Stores `return_count` into kpi_snapshots.

    If the token also has read_returns scope, fetches Return.totalQuantity
    for a more detailed `returned_items` metric. Falls back gracefully
    if that scope is missing.

    Previous implementation used ShopifyQL `FROM sales SHOW returns` which
    returned refund currency amounts, not counts (see INC-002).
    """
    from datetime import datetime, timedelta

    end_date = datetime.now() - timedelta(days=until_days)
    start_date = datetime.now() - timedelta(days=since_days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    # Statuses that indicate an order has a return
    return_statuses = {"RETURN_REQUESTED", "RETURN_IN_PROGRESS", "RETURNED"}

    daily_counts: dict[str, int] = {}  # date → number of orders with returns
    cursor = None
    total_returns_found = 0

    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query = """
        {
          orders(first: 50, query: "created_at:>=%s created_at:<=%s"%s) {
            edges {
              node {
                createdAt
                returnStatus
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        """ % (start_str, end_str, after_clause)

        try:
            data = _graphql_request(config, query)
        except RuntimeError as e:
            logger.warning("Returns GraphQL query failed: %s", e)
            return 0

        if "errors" in data:
            msgs = [e.get("message", str(e)) for e in data["errors"]]
            logger.warning("Returns GraphQL errors: %s", "; ".join(msgs))
            return 0

        orders_data = data.get("data", {}).get("orders", {})
        edges = orders_data.get("edges", [])

        for edge in edges:
            node = edge.get("node", {})
            created = node.get("createdAt", "")[:10]
            status = node.get("returnStatus", "")
            if not created:
                continue

            if status in return_statuses:
                daily_counts[created] = daily_counts.get(created, 0) + 1
                total_returns_found += 1

        page_info = orders_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    # Store as kpi_snapshots
    records = []
    for date_str, count in daily_counts.items():
        records.append((date_str, "return_count", float(count)))

    if records:
        insert_kpi_snapshots_bulk(conn, records)
    return total_returns_found


def fetch_customer_segments(config: SentinelConfig, conn, since_days: int = 30, until_days: int = 0) -> int:
    """Fetch first-time vs returning customer order counts per day.

    Uses GraphQL Order.customerJourneySummary.customerOrderIndex (1 = the
    customer's first-ever order), which is time-accurate per order — unlike
    customer.numberOfOrders, which is the customer's CURRENT lifetime count
    and would misclassify historical orders. ShopifyQL's returning_customers /
    first_time_customers columns no longer parse (INC-003).

    Stores per-day ORDER counts as first_time_customers / returning_customers
    (new-vs-returning by order, the same split Shopify admin reports). Orders
    whose journey summary is still null (computed async by Shopify, or no
    customer attached) are skipped.
    """
    start_str = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    end_str = (datetime.now() - timedelta(days=until_days)).strftime("%Y-%m-%d")

    first_time: dict[str, int] = {}
    returning: dict[str, int] = {}
    cursor = None
    total_orders = 0

    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query = """
        {
          orders(first: 250, query: "created_at:>=%s created_at:<=%s"%s) {
            edges {
              node {
                createdAt
                customerJourneySummary {
                  customerOrderIndex
                }
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        """ % (start_str, end_str, after_clause)

        try:
            data = _graphql_request(config, query)
        except RuntimeError as e:
            logger.warning("Customer segments query failed: %s", e)
            return 0

        if "errors" in data:
            msgs = [e.get("message", str(e)) for e in data["errors"]]
            logger.warning("Customer segments GraphQL errors: %s", "; ".join(msgs))
            return 0

        orders_data = data.get("data", {}).get("orders", {})
        for edge in orders_data.get("edges", []):
            node = edge.get("node", {})
            created = (node.get("createdAt") or "")[:10]
            index = (node.get("customerJourneySummary") or {}).get("customerOrderIndex")
            if not created or index is None:
                continue
            bucket = first_time if index == 1 else returning
            bucket[created] = bucket.get(created, 0) + 1
            total_orders += 1

        page_info = orders_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    records = [(d, "first_time_customers", float(c)) for d, c in first_time.items()]
    records += [(d, "returning_customers", float(c)) for d, c in returning.items()]
    if records:
        insert_kpi_snapshots_bulk(conn, records)
    return total_orders


def iter_fetch_stages(config: SentinelConfig, conn, since_days: int = 180):
    """Run all fetch stages, yielding a StageEvent as each completes.

    Records are written to conn as stages run; callers surface the events
    (CLI prints them, the MCP server maps them to progress notifications).
    """
    stages = [
        ("sales", lambda: fetch_sales(config, conn, since_days)),
        ("sessions", lambda: fetch_sessions(config, conn, since_days)),
        ("funnel", lambda: fetch_funnel(config, conn, since_days)),
        ("traffic", lambda: fetch_traffic_sources(config, conn, since_days)),
        ("themes", lambda: fetch_theme_updates(config, conn)),
        ("products", lambda: fetch_product_changes(config, conn)),
        ("price_rules", lambda: fetch_price_rules(config, conn)),
        ("product_performance", lambda: fetch_product_performance(config, conn, since_days)),
        ("channel_data", lambda: fetch_channel_data(config, conn, since_days)),
        ("inventory", lambda: fetch_inventory_levels(config, conn)),
        ("active_skus", lambda: fetch_active_sku_count(config, conn)),
        ("returns", lambda: fetch_returns(config, conn, since_days)),
        ("customer_segments", lambda: fetch_customer_segments(config, conn, since_days)),
    ]
    total = len(stages)
    for index, (name, fn) in enumerate(stages, 1):
        yield StageEvent(name=name, index=index, total=total, records=fn())


def fetch_all(config: SentinelConfig, conn, since_days: int = 180) -> dict[str, int]:
    """Orchestrate all data fetching. Returns counts per category."""
    return {ev.name: ev.records for ev in iter_fetch_stages(config, conn, since_days)}


def iter_history_stages(config: SentinelConfig, conn, since_days: int, until_days: int = 0):
    """Backfill variant of iter_fetch_stages: only the daily-history fetchers,
    scoped to the (since_days → until_days] window so resumable chunked
    backfills never re-fetch data they already have.

    Excludes the snapshot fetchers (themes, price rules, inventory, SKU count)
    and traffic sources (stored as a single 'aggregate' row that historical
    windows would clobber).
    """
    stages = [
        ("sales", lambda: fetch_sales(config, conn, since_days, until_days)),
        ("sessions", lambda: fetch_sessions(config, conn, since_days, until_days)),
        ("funnel", lambda: fetch_funnel(config, conn, since_days, until_days)),
        ("product_performance", lambda: fetch_product_performance(config, conn, since_days, until_days)),
        ("channel_data", lambda: fetch_channel_data(config, conn, since_days, until_days)),
        ("returns", lambda: fetch_returns(config, conn, since_days, until_days)),
        ("customer_segments", lambda: fetch_customer_segments(config, conn, since_days, until_days)),
    ]
    total = len(stages)
    for index, (name, fn) in enumerate(stages, 1):
        yield StageEvent(name=name, index=index, total=total, records=fn())
