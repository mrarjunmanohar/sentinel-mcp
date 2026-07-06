"""Guard: the MCP server's import graph must never write to stdout.

A stdio MCP server owns stdout for JSON-RPC frames — one stray print()
during import corrupts the protocol stream. This test imports the server-
side modules with a poisoned stdout and asserts the CLI-only modules
(reasoning) stay out of the server's import graph.

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_no_stdout
"""

import io
import sys


class _PoisonedStdout(io.TextIOBase):
    def write(self, s):
        raise AssertionError(f"stdout write during import: {s!r}")


SERVER_MODULES = [
    "sentinel.paths",
    "sentinel.config",
    "sentinel.models",
    "sentinel.db",
    "sentinel.date_utils",
    "sentinel.calculations",
    "sentinel.anomaly",
    "sentinel.fetcher",
    "sentinel.progress",
    "sentinel.report_builder",
    "sentinel.email_template",
    "sentinel.services",
    "sentinel.mcp_server",
]

# CLI-only modules that must never be pulled in by the server import graph.
FORBIDDEN_IN_SERVER = ["sentinel.reasoning", "sentinel.main", "sentinel.admin", "sentinel.backtest"]


def test_server_imports_are_stdout_clean():
    # Force fresh imports so module-level code actually runs under the poison.
    for name in list(sys.modules):
        if name == "sentinel" or name.startswith("sentinel."):
            del sys.modules[name]

    import importlib
    original = sys.stdout
    sys.stdout = _PoisonedStdout()
    try:
        for module in SERVER_MODULES:
            try:
                importlib.import_module(module)
            except ModuleNotFoundError as e:
                if module == "sentinel.mcp_server" and "mcp_server" in str(e):
                    continue  # not built yet (pre-Phase-2)
                raise
    finally:
        sys.stdout = original


def test_cli_only_modules_not_in_server_graph():
    for module in FORBIDDEN_IN_SERVER:
        assert module not in sys.modules, (
            f"{module} was imported by the server module graph — "
            "it is CLI-only and must stay out of the MCP server process"
        )


def _run():
    # Order matters: the import test populates sys.modules for the graph check.
    fns = [test_server_imports_are_stdout_clean, test_cli_only_modules_not_in_server_graph]
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
