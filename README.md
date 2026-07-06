# Sentinel — Shopify Intelligence for Claude Desktop

> **Pilot preview** — private repository, shared by invitation. Licensed under the [Elastic License 2.0](LICENSE): free to use and modify for your own store; not to be offered to third parties as a hosted or managed service. Feedback goes straight into the roadmap.

Sentinel is a self-contained insight and analytics intelligence layer for
Shopify, packaged as a **local MCP server** for Claude Desktop. It keeps a
local KPI history of your store (SQLite, daily aggregates only — no customer
data), runs deterministic anomaly detection over it, and hands Claude
structured, *correct* numbers so it can write your weekly/monthly performance
briefings, flag anomalies, build report dashboards, and draft the email to
your team.

**Why not just "chat with your Shopify data"?** Because ad-hoc LLM analytics
gets retail math wrong in quiet ways: averaged daily conversion rates
(Simpson's paradox), partial-period comparisons, refund amounts mistaken for
return counts. Sentinel encodes the correct math — conversion is always
`orders ÷ sessions` over the whole period, weeks are always completed Mon–Sun
vs prior Mon–Sun, months are completed calendar months with daily-normalized
anomaly scoring — and its `overall_status` (normal / warning / critical) is
computed by code, never by the model.

## What you can do

- *"Run my weekly briefing"* → fresh data sync, z-scored KPIs, styled HTML
  report saved locally, email draft via your Gmail/Outlook connector
- *"Anything unusual today?"* → daily anomaly check against 30-day baselines
- *"How did last month compare to the same month last year?"* → monthly YoY
  with backfilled history
- Schedule the weekly briefing with Claude Desktop's scheduled tasks — it runs
  unattended while the app is open

## Install (Claude Desktop)

> **Note:** this is a private repo — make sure `git` on your machine is authenticated as the invited GitHub account (easiest: `gh auth login`) so the `uvx --from git+https://…` install below can fetch it.

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/). Add to your
`claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "sentinel": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mrarjunmanohar/sentinel-mcp", "sentinel-mcp"],
      "env": {
        "SHOPIFY_SHOP": "yourstore.myshopify.com",
        "SHOPIFY_TOKEN": "shpat_...",
        "STORE_NAME": "Your Store",
        "CURRENCY": "USD",
        "STORE_DESCRIPTION": "one line about what you sell"
      }
    }
  }
}
```

Get the `SHOPIFY_TOKEN` by creating a read-only custom app on your store —
**[step-by-step guide](docs/SHOPIFY_TOKEN_GUIDE.md)** (≈5 minutes).

Restart Claude Desktop, then say: *"Backfill my Sentinel history"* (Claude
calls `sync_history` until done), then *"Run my weekly briefing"*.

Everything lives in a visible folder: `~/Sentinel/` — `data/` (per-store
SQLite), `reports/` (generated HTML), `stores.json` (multi-store config).
Override the location with the `SENTINEL_HOME` env var.

## Multiple stores

The env-configured store is the default. For more stores, add
`SHOPIFY_TOKEN_{SLUG}` env vars to the same config block, then tell Claude
*"add my store secondstore.myshopify.com"* — the `setup_store` tool validates
the token live and registers the store without the token ever entering the
chat. Every tool takes a `store` argument; `list_stores` shows the fleet.

## Tools

| Tool | What it does |
|---|---|
| `list_stores` | Configured stores + data coverage |
| `setup_store` | Register a store (live token validation, scope diagnostics) |
| `sync_store` | Fetch recent data (≤60 days, 13 stages, progress-reported) |
| `sync_history` | Chunked, resumable backfill (call until `done`) |
| `analyze_store` | Full quantitative analysis: z-scores, PoP, products, channels, programmatic status |
| `quick_check` | Fast daily anomaly pass |
| `generate_report_html` | Weave Claude's narrative into the styled HTML report; returns path + email digest |
| `maintenance` | Audit log, purge preview/execute (365-day floor) |

Plus three prompts (slash-command workflows): **Weekly Briefing**,
**Monthly Briefing**, **Anomaly Check**.

## Privacy

- Your store data rests **only on your machine** (daily aggregates; no
  customer names, emails, order IDs, or IPs are ever stored).
- Aggregate KPIs enter your Claude conversation when you ask for analysis —
  the same trust boundary as pasting numbers into chat. On consumer Claude
  plans, review your training preference in Privacy Settings (or use a
  Claude for Work plan).
- The MCP server makes network calls only to your own store's Shopify Admin
  API. No telemetry, no third-party services.

## License

Sentinel is source-available under the [Elastic License 2.0](LICENSE)
(licensor: Arjun Manohar, JAAX Labs). In plain terms:

- **Merchants & developers** — free to use, copy, modify, and self-host
  Sentinel for your own store(s). No strings.
- **Not allowed** — offering Sentinel to third parties as a hosted or managed
  service, or circumventing license-key functionality.
- **Agencies** — running Sentinel on behalf of your clients' stores requires a
  commercial license. Contact team@jaaxlabs.com.

Contributions are accepted under the same license, with Arjun Manohar,
JAAX Labs as licensor.

## Developer CLI

The same core powers a CLI for debugging and maintenance:

```bash
python3 -m venv sentinel/.venv && source sentinel/.venv/bin/activate && pip install -e .
sentinel --store yourstore --mode weekly        # report → ~/Sentinel/reports/
sentinel --mode fetch-history --days 730        # backfill
sentinel --mode backtest                        # threshold validation
python3 -m sentinel.admin                       # interactive store admin
for t in sentinel/tests/test_*.py; do python3 -m "sentinel.tests.$(basename "${t%.py}")"; done
```

The CLI's LLM narrative uses `ANTHROPIC_API_KEY` from `~/Sentinel/.env`; the
MCP server never needs an API key — Claude Desktop is the narrator.
