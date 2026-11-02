import os
import time
import gspread
from dotenv import load_dotenv

from archive_tool import get_archive_config

load_dotenv()

# Google Sheets occasionally answers 502/503/500 ("try again in 30 seconds")
# or 429 (quota) in the middle of a big write. write_to_sheets() clears the
# tab before writing, so one unretried failure leaves a production tab
# half-filled (happened 2026-10-05: main_metrics_outbrain stuck at 70k of
# 246k rows). Every write call goes through _retry.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _retry(fn, *args, attempts=6, **kwargs):
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status not in _RETRY_STATUS or i == attempts - 1:
                raise
            wait = min(30 * (2 ** i), 240)
            print(f"Google Sheets returned {status}; retrying in {wait}s (attempt {i + 2}/{attempts})...")
            time.sleep(wait)

# Tabs whose live copy is split against an archive spreadsheet (see archive_tool.py).
# Rows older than the stored cutoff get silently dropped here before writing, so
# even an accidental wide historical pull can never reintroduce old data into the
# live sheet and desync it from what the dashboards expect.
ARCHIVE_GUARDED_TABS = {"main_metrics", "main_metrics_outbrain"}

SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
COLUMNS = [
    "Date",
    "Campaign ID",
    "Campaign name",
    "Ad set ID",
    "Ad ID",
    "Ad set name",
    "Ad name",
    "Media type",
    "Start Date",
    "End Date",
    "Spend",
    "Impressions",
    "Clicks",
    "CPM",
    "CPC",
    "CTR",
    "Leads",
    "3-second video plays",
    "Video average play time",
    "Video plays at 25%",
    "Video plays at 50%",
    "Video plays at 75%",
    "Video plays at 100%",
    "Registration Rate",
    "Channel",
    "URL",
]


def write_to_sheets(rows, sheet_name="Sheet1", spreadsheet_id=None):
    sa_path = os.path.join(os.path.dirname(__file__), SERVICE_ACCOUNT_FILE)
    if not os.path.exists(sa_path):
        raise FileNotFoundError(
            f"Service account file not found at: {sa_path}\n"
            "Follow the setup instructions to create one."
        )

    sheet_id = spreadsheet_id or SPREADSHEET_ID
    gc = gspread.service_account(filename=sa_path)
    spreadsheet = gc.open_by_key(sheet_id)

    if sheet_name in ARCHIVE_GUARDED_TABS:
        cfg = get_archive_config(spreadsheet)
        if cfg:
            cutoff = cfg["cutoff"]
            before = len(rows)
            rows = [r for r in rows if not r.get("Date") or r["Date"] >= cutoff]
            dropped = before - len(rows)
            if dropped:
                print(f"Archive guard: dropped {dropped} row(s) dated before {cutoff} "
                      f"(already archived — see archive_tool.py) from '{sheet_name}'.")
            if before > 0 and not rows:
                # Every fetched row predates the cutoff — writing an empty result would
                # sheet.clear() the live tab and wipe out its current legitimate data.
                # Skip the write entirely instead of doing that.
                print(f"Archive guard: all {before} fetched row(s) for '{sheet_name}' are "
                      f"dated before the cutoff ({cutoff}) — nothing to write, skipping to "
                      f"avoid wiping the live sheet. Use the Archive & Trim tool if you need "
                      f"older data added to the archive.")
                return

    try:
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=5000, cols=len(COLUMNS))

    _retry(sheet.clear)

    all_rows = [[row.get(col, "") for col in COLUMNS] for row in rows]

    # Size the grid up front, then write every chunk to a FIXED row position
    # (not append_rows): a retried chunk then overwrites the same rows instead
    # of appending a duplicate when Google saved it but still answered 5xx.
    _retry(sheet.resize, rows=max(len(all_rows) + 1, 2), cols=len(COLUMNS))
    BATCH = 10_000
    _retry(sheet.update, "A1", [COLUMNS] + all_rows[:BATCH], value_input_option="USER_ENTERED")
    for i in range(BATCH, len(all_rows), BATCH):
        _retry(sheet.update, f"A{i + 2}", all_rows[i:i + BATCH], value_input_option="USER_ENTERED")
        print(f"  ... {min(i + BATCH, len(all_rows))}/{len(all_rows)} rows written")

    # USER_ENTERED lets Google Sheets auto-interpret values, which can cause
    # small numbers (e.g. 1.0) to be stored with Date format ("1900-01-01").
    # Enforce NUMBER format on every numeric column after writing to prevent this.
    NUMERIC_COLS = {
        "Spend", "Impressions", "Clicks", "CPM", "CPC", "CTR", "Leads",
        "3-second video plays", "Video average play time",
        "Video plays at 25%", "Video plays at 50%",
        "Video plays at 75%", "Video plays at 100%", "Registration Rate",
    }
    num_rows = len(rows) + 1  # +1 for header row
    requests = []
    for col_name in NUMERIC_COLS:
        if col_name not in COLUMNS:
            continue
        col_idx = COLUMNS.index(col_name)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet.id,
                    "startRowIndex": 1,       # skip header
                    "endRowIndex": num_rows,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    if requests:
        _retry(sheet.spreadsheet.batch_update, {"requests": requests})

    print(f"Sheet '{sheet_name}' updated: {len(rows)} rows written.")


def write_table(rows, columns, sheet_name, spreadsheet_id, numeric_cols=()):
    """Replace a tab with `rows` (dicts keyed by `columns`). Used for sheets
    that don't share main_metrics' fixed COLUMNS schema (e.g. the WhatsApp sheet).
    Not archive-guarded: only use it for tabs outside ARCHIVE_GUARDED_TABS."""
    if sheet_name in ARCHIVE_GUARDED_TABS:
        raise ValueError(f"'{sheet_name}' is archive-guarded; use write_to_sheets().")
    sa_path = os.path.join(os.path.dirname(__file__), SERVICE_ACCOUNT_FILE)
    gc = gspread.service_account(filename=sa_path)
    spreadsheet = gc.open_by_key(spreadsheet_id)
    try:
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=max(len(rows) + 10, 100), cols=len(columns))

    _retry(sheet.clear)
    # RAW: dates stay ISO text (gviz reads them as strings; no locale re-parsing).
    _retry(sheet.update, "A1", [list(columns)] + [[row.get(c, "") for c in columns] for row in rows],
           value_input_option="RAW")

    requests = []
    for col_name in numeric_cols:
        col_idx = list(columns).index(col_name)
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": len(rows) + 1,
                          "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    if requests:
        _retry(spreadsheet.batch_update, {"requests": requests})
    print(f"Sheet '{sheet_name}' updated: {len(rows)} rows written.")
