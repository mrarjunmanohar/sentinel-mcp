# Quick-Start Guide

> **This guide described the pre-MCP Gmail-draft workflow and is superseded.**
> Sentinel is now a local MCP server for Claude Desktop — installation,
> Shopify token setup, multi-store configuration, and the developer CLI are
> all documented in the [README](../README.md), with the token walkthrough in
> [SHOPIFY_TOKEN_GUIDE.md](SHOPIFY_TOKEN_GUIDE.md).

The short version:

1. Add Sentinel to `claude_desktop_config.json` (uvx command + your
   `SHOPIFY_SHOP` / `SHOPIFY_TOKEN` env vars) — see the README.
2. Restart Claude Desktop and say: *"Backfill my Sentinel history."*
3. Then: *"Run my weekly briefing and draft the email to my team."*
4. Optional: create a Claude Desktop scheduled task with that same prompt to
   get the briefing every Monday while the app is open.

If you'd like to share the report with your team then, Claude drafts the email through your own Gmail or
Outlook connector; Sentinel just produces the report and digest. Ensure, your email app is connected to Claude. 
