"""
Ad Data Automation
==================
Fetches data from Meta Ads and Google Ads, then writes to Google Sheets.

Usage:
  python main.py                          # yesterday's data, all sources
  python main.py --start 2024-01-01 --end 2024-01-31   # custom date range
  python main.py --source meta            # single source only
  python main.py --source google
  python main.py --source whatsapp        # GA4 WhatsApp slider -> separate sheet
                                          # (WHATSAPP_SHEETS_ID; always full history,
                                          #  ignores --start/--end and --target)
"""

import argparse
import os
import sys
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

from fetchers.meta import fetch_meta_data
from fetchers.google_ads import fetch_google_ads_data
from fetchers.outbrain import fetch_outbrain_data
from fetchers.tiktok import fetch_tiktok_data
from fetchers.whatsapp import (fetch_whatsapp_data, TABLES as WHATSAPP_TABLES,
                               load_report_snapshots, SNAPSHOT_COLUMNS)
from sheets_writer import write_to_sheets, write_table

load_dotenv()

TARGETS = {
    "test": {
        "spreadsheet_id_env": "GOOGLE_SHEETS_ID",
        "sheet_main": "Sheet1",
        "sheet_outbrain": "outbrain",
    },
    "production": {
        "spreadsheet_id_env": "GOOGLE_SHEETS_ID_PRODUCTION",
        "sheet_main": "main_metrics",
        "sheet_outbrain": "main_metrics_outbrain",
    },
}


def main():
    parser = argparse.ArgumentParser(description="Fetch ad data and write to Google Sheets.")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument(
        "--source",
        default="all",
        help=("Data source: meta | google | outbrain | tiktok | whatsapp | all, "
              "or a comma-separated list (e.g. meta,google). Default: all"),
    )
    parser.add_argument(
        "--target",
        choices=["test", "production"],
        default="test",
        help="Destination sheet (default: test)",
    )
    args = parser.parse_args()

    target_cfg = TARGETS[args.target]
    spreadsheet_id = os.getenv(target_cfg["spreadsheet_id_env"])
    if not spreadsheet_id:
        print(f"Error: env var {target_cfg['spreadsheet_id_env']} is not set.")
        sys.exit(1)
    sheet_main = target_cfg["sheet_main"]
    sheet_outbrain = target_cfg["sheet_outbrain"]
    print(f"Target: {args.target.upper()} → spreadsheet {spreadsheet_id} (tabs: {sheet_main}, {sheet_outbrain})")

    if bool(args.start) != bool(args.end):
        print("Error: provide both --start and --end, or neither.")
        sys.exit(1)

    if args.start:
        start_date, end_date = args.start, args.end
    else:
        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = end_date = yesterday

    # Parse --source as a comma-separated set. "all" expands to every source.
    selected = {s.strip().lower() for s in args.source.split(",") if s.strip()}
    if not selected or "all" in selected:
        selected = {"meta", "google", "outbrain", "tiktok", "whatsapp"}
    unknown = selected - {"meta", "google", "outbrain", "tiktok", "whatsapp"}
    if unknown:
        print(f"Error: unknown source(s): {sorted(unknown)}. "
              "Allowed: meta, google, outbrain, tiktok, whatsapp, all.")
        sys.exit(1)

    print(f"\nFetching data: {start_date} to {end_date} | "
          f"Source: {', '.join(sorted(selected))}\n")

    all_data = []

    if "meta" in selected:
        print("[ Meta Ads ] Fetching...")
        try:
            data = fetch_meta_data(start_date, end_date)
            all_data.extend(data)
            print(f"[ Meta Ads ] {len(data)} rows fetched.")
        except Exception as e:
            print(f"[ Meta Ads ] ERROR: {e}")

    if "google" in selected:
        print("[ Google Ads ] Fetching...")
        try:
            data = fetch_google_ads_data(start_date, end_date)
            all_data.extend(data)
            print(f"[ Google Ads ] {len(data)} rows fetched.")
        except Exception as e:
            print(f"[ Google Ads ] ERROR: {e}")

    if "outbrain" in selected:
        print("[ Outbrain ] Fetching...")
        try:
            data = fetch_outbrain_data(start_date, end_date)
            all_data.extend(data)
            print(f"[ Outbrain ] {len(data)} rows fetched.")
        except Exception as e:
            print(f"[ Outbrain ] ERROR: {e}")

    if "tiktok" in selected:
        print("[ TikTok ] Fetching...")
        try:
            data = fetch_tiktok_data(start_date, end_date)
            all_data.extend(data)
            print(f"[ TikTok ] {len(data)} rows fetched.")
        except Exception as e:
            print(f"[ TikTok ] ERROR: {e}")

    if "whatsapp" in selected:
        run_whatsapp()

    if not all_data:
        if selected - {"whatsapp"}:
            print("\nNo data returned. Sheet not updated.")
        return

    outbrain_rows = [r for r in all_data if r.get("Channel") == "Outbrain"]
    other_rows = [r for r in all_data if r.get("Channel") != "Outbrain"]

    if other_rows:
        srcs = ", ".join(sorted({r.get("Channel", "?") for r in other_rows}))
        print(f"\n[ {srcs} ] Writing {len(other_rows)} rows to '{sheet_main}'...")
        write_to_sheets(other_rows, sheet_name=sheet_main, spreadsheet_id=spreadsheet_id)

    if outbrain_rows:
        print(f"[ Outbrain ] Writing {len(outbrain_rows)} rows to '{sheet_outbrain}'...")
        write_to_sheets(outbrain_rows, sheet_name=sheet_outbrain, spreadsheet_id=spreadsheet_id)

    print("Done!")


def run_whatsapp():
    """GA4 WhatsApp slider -> its own spreadsheet (WHATSAPP_SHEETS_ID), never
    main_metrics. Counts only: see fetchers/whatsapp.py."""
    sheet_id = os.getenv("WHATSAPP_SHEETS_ID")
    if not sheet_id:
        print("[ WhatsApp ] Skipped: WHATSAPP_SHEETS_ID is not set "
              "(create the sheet, share it with the service account, paste its ID).")
        return
    print("[ WhatsApp ] Fetching GA4 (full history) -> its own sheet, not main_metrics...")
    try:
        tables = fetch_whatsapp_data()
        text_cols = {"Date", "Campaign", "UTM campaign", "Landing", "Metric", "Key", "Value"}
        for tab, columns in WHATSAPP_TABLES.items():
            write_table(tables[tab], columns, tab, sheet_id,
                        numeric_cols=[c for c in columns if c not in text_cols])
        # The colleague's frozen weekly Report numbers (source of truth for
        # the weeks they cover). Skipped, not failed, if the file isn't there.
        snaps, path = load_report_snapshots()
        if snaps is None:
            print(f"[ WhatsApp ] Report snapshots not found at {path} - keeping the sheet's last copy.")
        else:
            write_table(snaps, SNAPSHOT_COLUMNS, "wa_report_snapshots", sheet_id)
    except Exception as e:
        print(f"[ WhatsApp ] ERROR: {e}")


if __name__ == "__main__":
    main()
