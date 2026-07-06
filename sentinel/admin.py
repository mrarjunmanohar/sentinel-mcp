"""Interactive admin CLI for Shopify AI Sentinel.

Run with:
    python3 -m sentinel.admin
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .config import get_store_config
from .db import get_connection, init_db
from .main import fetch_history, run
from .paths import data_dir, db_path_for, reports_dir, stores_path

STORES_PATH = stores_path()
DATA_DIR = data_dir()


def _latest_report(slug: str) -> Path | None:
    """Newest report HTML for a store (reports/{slug}/*.html, legacy fallback)."""
    report_dir = reports_dir(slug)
    if report_dir.is_dir():
        reports = sorted(report_dir.glob("*.html"), key=lambda p: p.stat().st_mtime)
        if reports:
            return reports[-1]
    for legacy in (DATA_DIR / f"{slug}_last_briefing.html", DATA_DIR / "last_briefing.html"):
        if legacy.exists():
            return legacy
    return None

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _hr() -> None:
    print("─" * 56)


def _prompt(label: str, default: str = "") -> str:
    """Prompt with optional default. Returns stripped input or default."""
    if default:
        val = input(f"  {label} [{default}]: ").strip()
        return val if val else default
    else:
        return input(f"  {label}: ").strip()


def _load_stores() -> dict:
    """Load stores.json. Returns empty dict if missing."""
    try:
        with open(STORES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_stores(stores: dict) -> None:
    """Atomically write stores to stores.json."""
    tmp = Path(str(STORES_PATH) + ".tmp")
    tmp.write_text(json.dumps(stores, indent=2))
    os.replace(tmp, STORES_PATH)


def _pick_store(stores: dict) -> str | None:
    """Ask user to pick a store slug. Returns slug or None if cancelled."""
    slugs = sorted(stores.keys())
    if not slugs:
        print("  No stores configured. Add one with option [1].")
        return None
    if len(slugs) == 1:
        return slugs[0]
    print()
    for i, slug in enumerate(slugs, 1):
        print(f"  [{i}] {slug}")
    choice = input("\n  Pick a store (number): ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(slugs):
            return slugs[idx]
    except ValueError:
        pass
    print("  Invalid choice.")
    return None


def _get_store_status(slug: str, store_cfg: dict) -> dict:
    """Return status info for a store: db_days, db_size_mb, last_report, status."""
    db_path = str(db_path_for(slug))
    result = {
        "db_days": 0,
        "db_size_mb": 0.0,
        "last_report_date": None,
        "last_report_path": None,
        "status": "NO DATA",
    }

    # DB size
    if os.path.exists(db_path):
        result["db_size_mb"] = os.path.getsize(db_path) / 1_000_000

    # DB date range
    try:
        init_db(db_path)
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT MIN(date), MAX(date) FROM kpi_snapshots WHERE date != 'aggregate'"
            ).fetchone()
        if row and row[0] and row[1]:
            min_dt = datetime.strptime(row[0], "%Y-%m-%d")
            max_dt = datetime.strptime(row[1], "%Y-%m-%d")
            result["db_days"] = (max_dt - min_dt).days + 1
    except Exception:
        pass

    # Last report HTML (reports/{slug}/*.html, with legacy data/ fallback)
    report_path = _latest_report(slug)
    if report_path:
        result["last_report_path"] = report_path
        mtime = os.path.getmtime(report_path)
        result["last_report_date"] = datetime.fromtimestamp(mtime)

    # Status
    days = result["db_days"]
    if days >= 30:
        result["status"] = "OK"
    elif days > 0:
        result["status"] = "PENDING"
    else:
        result["status"] = "NO DATA"

    return result


# ─── Actions ──────────────────────────────────────────────────────────────────

def action_add_store() -> None:
    """Guided prompts to add a new store to stores.json."""
    print("\n  ADD NEW STORE")
    _hr()

    stores = _load_stores()

    slug = _prompt("Store slug (e.g. mystore)")
    if not slug:
        print("  Cancelled.")
        return
    if slug in stores:
        print(f"  Store '{slug}' already exists.")
        return

    shop = _prompt("Shop domain (e.g. mystore.myshopify.com)")
    token = _prompt("Shopify Admin API token (shpat_...)")
    store_name = _prompt("Store name (display)", default=slug.replace("-", " ").title())
    currency = _prompt("Currency [USD/INR/GBP/EUR]", default="USD").upper()
    description = _prompt("Store description (one line for AI)")
    sensitivity = _prompt("Alert sensitivity [LOW/MED/HIGH]", default="MED").upper()
    if sensitivity not in ("LOW", "MED", "HIGH"):
        sensitivity = "MED"

    emails_raw = _prompt("Recipient emails (comma-separated)")
    recipients = [e.strip() for e in emails_raw.split(",") if e.strip()]

    do_backfill = _prompt("Backfill history on save? [Y/n]", default="Y").upper()
    backfill_days = 700
    if do_backfill == "Y":
        days_str = _prompt("Days to backfill", default="700")
        try:
            backfill_days = int(days_str)
        except ValueError:
            backfill_days = 700

    stores[slug] = {
        "shop": shop,
        "token": token,
        "store_name": store_name,
        "currency": currency,
        "store_description": description,
        "email_recipients": recipients,
        "alert_sensitivity": sensitivity,
    }

    _save_stores(stores)
    print(f"\n  {store_name} added.")

    if do_backfill == "Y":
        print(f"  Starting {backfill_days}-day backfill...")
        try:
            config = get_store_config(slug)
            init_db(config.db_path)
            fetch_history(config, days=backfill_days)
            print("  Backfill complete.")
        except Exception as e:
            print(f"  Backfill failed: {e}")


def action_list_stores() -> None:
    """Print a status table for all configured stores."""
    stores = _load_stores()

    print()
    _hr()
    print(f"  {'STORE':<18} {'LAST REPORT':<20} {'STATUS':<10} {'DB SIZE'}")
    _hr()

    if not stores:
        print("  No stores configured.")
        _hr()
        return

    for slug in sorted(stores.keys()):
        st = _get_store_status(slug, stores[slug])

        last_run = "Never"
        if st["last_report_date"]:
            last_run = st["last_report_date"].strftime("%b %d %H:%M")

        db_size = f"{st['db_size_mb']:.1f}MB" if st["db_size_mb"] > 0 else "—"
        status = st["status"]

        print(f"  {slug:<18} {last_run:<20} {status:<10} {db_size}")

    _hr()


def action_run_report() -> None:
    """Run weekly report for a selected store."""
    stores = _load_stores()
    slug = _pick_store(stores)
    if not slug:
        return

    print(f"\n  Running weekly report for {slug}...")
    try:
        config = get_store_config(slug)
        run(config=config, mode="weekly", store_slug=slug)
    except Exception as e:
        print(f"  Report failed: {e}")
        return

    # Offer to open HTML
    report_path = _latest_report(slug)
    if report_path:
        open_it = input("\n  Open report in browser? [Y/n]: ").strip().upper()
        if open_it != "N":
            _open_in_browser(report_path)


def action_fetch_history() -> None:
    """Fetch / backfill historical data for a selected store."""
    stores = _load_stores()
    slug = _pick_store(stores)
    if not slug:
        return

    days_str = _prompt("Days to fetch", default="700")
    try:
        days = int(days_str)
    except ValueError:
        days = 700

    print(f"\n  Fetching {days} days for {slug}...")
    try:
        config = get_store_config(slug)
        init_db(config.db_path)
        fetch_history(config, days=days)
    except Exception as e:
        print(f"  Fetch failed: {e}")


def action_view_last_report() -> None:
    """Open the last generated HTML report in a browser."""
    stores = _load_stores()
    slug = _pick_store(stores)
    if not slug:
        return

    report_path = _latest_report(slug)

    if not report_path:
        print(f"  No report found. Run option [3] to generate one.")
        return

    _open_in_browser(report_path)


def _open_in_browser(path: Path) -> None:
    """Open a local HTML file in the default browser."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(path)], check=True)
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
        print(f"  Opened {path.name}")
    except Exception as e:
        print(f"  Could not open browser: {e}")
        print(f"  Report saved at: {path}")


# ─── Menu ─────────────────────────────────────────────────────────────────────

def main_menu() -> bool:
    """Print menu, get choice, dispatch. Returns False to exit."""
    print("\n\nSENTINEL ADMIN")
    _hr()
    print("  [1] Add new store")
    print("  [2] List stores + status")
    print("  [3] Run report for store")
    print("  [4] Fetch / backfill history")
    print("  [5] View last report")
    print("  [6] Exit")
    _hr()

    choice = input("  Choice: ").strip()

    if choice == "1":
        action_add_store()
    elif choice == "2":
        action_list_stores()
    elif choice == "3":
        action_run_report()
    elif choice == "4":
        action_fetch_history()
    elif choice == "5":
        action_view_last_report()
    elif choice == "6":
        return False
    else:
        print("  Invalid choice.")

    return True


def main() -> None:
    """Entry point for the interactive admin CLI."""
    print("\nSentinel Admin — type Ctrl+C to quit at any time")
    try:
        while main_menu():
            pass
    except KeyboardInterrupt:
        print("\n\n  Bye.")


if __name__ == "__main__":
    main()
