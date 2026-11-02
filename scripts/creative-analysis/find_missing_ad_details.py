"""
Compare 'important_rows' tab against 'Ad Details' tab and report which ads:
  (a) are missing entirely from Ad Details
  (b) exist in Ad Details but have any of the Gemini columns blank
       (Hook category, Hook text, POV, Setting, Edit pace,
        Format & duration, Person on screen, Main message)
"""
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / "ads_automation" / ".env")

sys.path.insert(0, str(HERE))
from analyze_creatives import SHEET_ID, SA_PATH, normalize_ad_name

IMPORTANT_TAB = "important_rows"
DETAILS_TAB = "Ad Details"

GEMINI_COLS = [
    "Hook category", "Hook text", "POV", "Setting", "Edit pace",
    "Format & duration", "Person on screen", "Main message",
]


def main():
    gc = gspread.service_account(filename=str(SA_PATH))
    sh = gc.open_by_key(SHEET_ID)

    imp = sh.worksheet(IMPORTANT_TAB).get_all_values()
    det = sh.worksheet(DETAILS_TAB).get_all_values()

    if not imp or not det:
        print("One of the tabs is empty.")
        return

    imp_header, imp_rows = imp[0], imp[1:]
    det_header, det_rows = det[0], det[1:]

    # Find Ad Name column in each tab (case-insensitive, first match)
    def find_col(header, name):
        for i, h in enumerate(header):
            if h.strip().lower() == name.lower():
                return i
        return -1

    imp_name_idx = find_col(imp_header, "Ad Name")
    det_name_idx = find_col(det_header, "Ad Name")
    if imp_name_idx < 0 or det_name_idx < 0:
        print(f"Could not find 'Ad Name' column.")
        print(f"  important_rows header: {imp_header}")
        print(f"  Ad Details header:    {det_header}")
        return

    gemini_idx = {c: find_col(det_header, c) for c in GEMINI_COLS}
    missing_cols = [c for c, i in gemini_idx.items() if i < 0]
    if missing_cols:
        print(f"Gemini columns not found in Ad Details header: {missing_cols}")
        print(f"  Ad Details header: {det_header}")
        return

    # Build lookup of Ad Details by both raw and normalized name
    det_by_name = {}
    for r in det_rows:
        if det_name_idx >= len(r):
            continue
        nm = (r[det_name_idx] or "").strip()
        if not nm:
            continue
        det_by_name[nm.lower()] = r
        det_by_name[normalize_ad_name(nm).lower()] = r

    important_names = []
    for r in imp_rows:
        if imp_name_idx >= len(r):
            continue
        nm = (r[imp_name_idx] or "").strip()
        if nm:
            important_names.append(nm)

    truly_missing = []
    incomplete = []  # (name, [blank_cols])
    complete = 0

    for nm in important_names:
        keys = [nm.lower(), normalize_ad_name(nm).lower()]
        row = next((det_by_name[k] for k in keys if k in det_by_name), None)
        if row is None:
            truly_missing.append(nm)
            continue
        blanks = []
        for c in GEMINI_COLS:
            i = gemini_idx[c]
            v = (row[i].strip() if i < len(row) else "")
            if not v:
                blanks.append(c)
        if blanks:
            incomplete.append((nm, blanks))
        else:
            complete += 1

    total = len(important_names)
    print(f"important_rows ads:                   {total}")
    print(f"  fully populated in Ad Details:      {complete}")
    print(f"  in Ad Details but missing columns:  {len(incomplete)}")
    print(f"  not in Ad Details at all:           {len(truly_missing)}")
    print()

    if truly_missing:
        print(f"--- Missing from Ad Details ({len(truly_missing)}) ---")
        for nm in truly_missing:
            print(f"  {nm}")
        print()

    if incomplete:
        print(f"--- In Ad Details but with blank columns ({len(incomplete)}) ---")
        for nm, blanks in incomplete:
            print(f"  {nm}")
            print(f"     blank: {', '.join(blanks)}")


if __name__ == "__main__":
    main()
