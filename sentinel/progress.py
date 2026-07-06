"""Progress protocol shared by the CLI and the MCP server.

Long operations (the 13-stage Shopify fetch) yield one StageEvent per
completed stage. The CLI prints them; the MCP server maps them to
ctx.report_progress()/ctx.info() notifications. Sync generators only —
no threads, no callbacks across event loops.
"""

from dataclasses import dataclass


@dataclass
class StageEvent:
    name: str      # stage identifier, e.g. "sales"
    index: int     # 1-based position
    total: int     # total number of stages
    records: int   # records written by this stage

    def __str__(self) -> str:
        return f"[{self.index}/{self.total}] {self.name}: {self.records} records"
