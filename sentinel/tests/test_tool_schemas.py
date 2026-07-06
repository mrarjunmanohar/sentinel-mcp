"""Contract tests for the MCP server's tool/prompt surface.

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_tool_schemas
"""

import sys

import anyio

from sentinel import mcp_server

EXPECTED_TOOLS = {
    "list_stores",
    "setup_store",
    "sync_store",
    "sync_history",
    "analyze_store",
    "quick_check",
    "generate_report_html",
    "maintenance",
}

EXPECTED_PROMPTS = {"weekly_briefing", "monthly_briefing", "anomaly_check"}

# CLI-only modules that must never be in the server import graph.
FORBIDDEN = ["sentinel.reasoning", "sentinel.main", "sentinel.admin", "sentinel.backtest"]


def test_tool_names_and_count():
    tools = anyio.run(mcp_server.mcp.list_tools)
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS, names
    assert len(tools) <= 9, len(tools)  # each tool description costs context


def test_generate_report_html_schema():
    tools = {t.name: t for t in anyio.run(mcp_server.mcp.list_tools)}
    schema = tools["generate_report_html"].inputSchema
    assert schema["required"] == ["primary_insight"], schema.get("required")
    props = set(schema["properties"])
    assert {"store", "mode", "compare", "critical_insight", "product_insight",
            "campaign_action", "inventory_action", "next_week_priorities",
            "sales_channel_note"} <= props, props
    # overall_status must NOT be accepted from the narrator (INC-001).
    assert "overall_status" not in props


def test_analyze_store_schema():
    tools = {t.name: t for t in anyio.run(mcp_server.mcp.list_tools)}
    props = set(tools["analyze_store"].inputSchema["properties"])
    assert props == {"store", "mode", "compare", "sync"}, props


def test_prompts_registered():
    prompts = anyio.run(mcp_server.mcp.list_prompts)
    names = {p.name for p in prompts}
    assert names == EXPECTED_PROMPTS, names


def test_cli_modules_not_imported():
    for module in FORBIDDEN:
        assert module not in sys.modules, (
            f"{module} is in the MCP server import graph — it is CLI-only"
        )


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
