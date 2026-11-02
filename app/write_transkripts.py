"""
Reads all unique Ad names from main_metrics and writes them to the Transkripts tab.
Columns: Ad Name | Channel | Media Type | Transcript
"""

import gspread

SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "__SHEET_ID__"


def main():
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sp = gc.open_by_key(SPREADSHEET_ID)

    # Read source data
    print("Reading main_metrics...")
    src = sp.worksheet("main_metrics")
    records = src.get_all_records()
    print(f"  {len(records)} rows found.")

    # Deduplicate by Ad name, preserving first-seen Channel + Media type
    seen = {}
    for r in records:
        name = str(r.get("Ad name", "")).strip()
        if not name:
            continue
        if name not in seen:
            seen[name] = {
                "channel": str(r.get("Channel", "")).strip(),
                "media_type": str(r.get("Media type", "")).strip(),
            }

    print(f"  {len(seen)} unique ad names.")

    # Write to Transkripts tab
    try:
        sheet = sp.worksheet("Transkripts")
    except gspread.exceptions.WorksheetNotFound:
        sheet = sp.add_worksheet(title="Transkripts", rows=500, cols=4)

    sheet.clear()

    rows = [["Ad Name", "Channel", "Media Type", "Transcript"]]
    for name in sorted(seen.keys()):
        rows.append([name, seen[name]["channel"], seen[name]["media_type"], ""])

    sheet.update(range_name="A1", values=rows, value_input_option="USER_ENTERED")
    print(f"Done — {len(seen)} ad names written to Transkripts tab.")


if __name__ == "__main__":
    main()
