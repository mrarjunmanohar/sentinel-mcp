# Getting your Shopify Admin API token for Sentinel

Sentinel reads your store's analytics through Shopify's Admin API. You create a
private "custom app" on your own store and hand its token to Sentinel — no
third party ever sees your data. Takes about 5 minutes.

## Step 1 — Enable custom app development (one time)

1. In your Shopify admin, go to **Settings → Apps and sales channels**.
2. Click **Develop apps** (top right).
3. If prompted, click **Allow custom app development** (twice — Shopify asks you to confirm).

> Only the store owner (or a staff member with full app permissions) can do this.

## Step 2 — Create the app

1. Click **Create an app**.
2. Name it `Sentinel` (any name works). Select yourself as the app developer.
3. Click **Create app**.

## Step 3 — Grant the Admin API scopes

1. On the app page, open the **Configuration** tab → **Admin API integration** → **Configure**.
2. Tick exactly these scopes (search the list):

   | Scope | Why Sentinel needs it |
   |---|---|
   | `read_orders` | Order counts, product performance, return counts |
   | `read_products` | Product change events, inventory levels, SKU counts |
   | `read_reports` | ShopifyQL analytics (sales, sessions, conversion funnel) |
   | `read_themes` | Theme-change events (context for anomaly explanations) |
   | `read_price_rules` | Discount/price-rule events |
   | `read_inventory` | Inventory quantities |

3. Click **Save**.

Sentinel is read-only by design — never grant any `write_*` scope.

## Step 4 — Install and copy the token

1. Open the **API credentials** tab.
2. Click **Install app** → **Install**.
3. Under **Admin API access token**, click **Reveal token once** and copy it.
   It starts with `shpat_` (or `shpca_` if the app was created through the
   Shopify Partner Dashboard instead of the store admin — both work).
   **Shopify shows it only once** — if you lose it, uninstall and reinstall
   the app to get a new one.

## Step 5 — Give the token to Sentinel

**Recommended (keeps the token out of your Claude conversation):** put it in the
Sentinel MCP server's environment, in your `claude_desktop_config.json`:

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
        "STORE_DESCRIPTION": "sells premium widgets"
      }
    }
  }
}
```

For **additional stores**, add `SHOPIFY_TOKEN_{SLUG}` variables (e.g.
`SHOPIFY_TOKEN_SECONDSTORE`) to the same `env` block, then tell Claude:
*"add my store secondstore.myshopify.com to Sentinel"* — the `setup_store`
tool finds the token in the environment and never sees it in chat.

You *can* paste the token directly in chat and let Claude call
`setup_store(admin_token=...)`, but be aware the token then transits the
conversation (Anthropic's servers). The environment route avoids that.

## Troubleshooting

| Error from `setup_store` | Fix |
|---|---|
| "Shopify rejected the token (HTTP 401/403)" | Token mis-copied, or the app isn't installed (Step 4). Reveal a fresh token and retry. |
| "analytics access failed" / mentions `read_reports` | The `read_reports` scope is missing (Step 3), or your Shopify plan doesn't include analytics API access. |
| "GraphQL errors: … ACCESS_DENIED …" during sync | A specific scope is missing — compare your app's scopes against the table above. |
| Partial data (e.g. no return counts) | `read_orders` missing. Sync reports these as warnings rather than failing. |
