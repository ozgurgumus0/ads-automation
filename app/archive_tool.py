"""
Archive & Trim
==============
Moves rows dated before a chosen cutoff out of the live production spreadsheet
(main_metrics, main_metrics_outbrain, New_Data) into the archive spreadsheet,
so the live sheet stays small while all history remains available (split
across archive + live, merged transparently by the dashboards at load time).

The current archive spreadsheet ID and cutoff date are stored in the
production spreadsheet's `_archive_config` tab (key/value, two rows:
`archive_sheet_id`, `cutoff`) — this is the single source of truth read by
this tool, by sheets_writer.py's write-time guardrail, and by both dashboard
HTML files, so changing the cutoff here never requires editing code elsewhere.

Usage (CLI):
  python archive_tool.py --new-cutoff 2026-09-01
"""

import os
import sys
from datetime import date, datetime

import gspread
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CONFIG_TAB = "_archive_config"
BATCH = 10_000

# (tab name, date column index) for every tab that gets archived. main_metrics /
# main_metrics_outbrain use ISO 'YYYY-MM-DD' in column A; New_Data uses M/D/YYYY
# in column C ("Lead Created Date") — confirmed against real sheet samples.
ISO_TABS = ["main_metrics", "main_metrics_outbrain"]
MDY_TAB = "New_Data"


def _client():
    sa_path = os.path.join(BASE_DIR, "service_account.json")
    return gspread.service_account(filename=sa_path)


def _production_spreadsheet(gc=None):
    gc = gc or _client()
    sheet_id = os.getenv("GOOGLE_SHEETS_ID_PRODUCTION")
    return gc.open_by_key(sheet_id)


def get_archive_config(spreadsheet=None, gc=None):
    """Reads the _archive_config tab. Returns {'archive_sheet_id', 'cutoff'} or
    None if the tab doesn't exist yet or is incomplete — callers should no-op
    gracefully in that case rather than error."""
    try:
        spreadsheet = spreadsheet or _production_spreadsheet(gc)
        ws = spreadsheet.worksheet(CONFIG_TAB)
        rows = ws.get_all_values()
    except Exception:
        return None
    cfg = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0]:
            cfg[row[0].strip()] = row[1].strip()
    if "archive_sheet_id" not in cfg or "cutoff" not in cfg:
        return None
    return cfg


def _set_cutoff(spreadsheet, new_cutoff_iso):
    ws = spreadsheet.worksheet(CONFIG_TAB)
    cell = ws.find("cutoff")
    ws.update_cell(cell.row, cell.col + 1, new_cutoff_iso)


def _parse_mdy(s):
    try:
        m, d, y = s.strip().split("/")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def _validate_cutoff(new_cutoff, current_cutoff):
    try:
        new_d = datetime.strptime(new_cutoff, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"'{new_cutoff}' is not a valid YYYY-MM-DD date.")
    if new_d > date.today():
        raise ValueError(f"Cutoff {new_cutoff} is in the future.")
    if current_cutoff:
        cur_d = datetime.strptime(current_cutoff, "%Y-%m-%d").date()
        if new_d <= cur_d:
            raise ValueError(
                f"New cutoff {new_cutoff} must be strictly after the current cutoff "
                f"{current_cutoff} — archive and live ranges can never overlap."
            )
    return new_d


def _iso_before(cutoff_iso):
    return lambda v: v < cutoff_iso


def _mdy_before(cutoff_iso):
    def check(v):
        dt = _parse_mdy(v)
        return dt is not None and dt.isoformat() < cutoff_iso
    return check


def _move_tab(live_sh, archive_sh, tab_name, date_col_idx, is_before_cutoff):
    """Splits a tab's live rows into to_move (before cutoff) / to_keep (on/after,
    or blank/unparseable date — kept defensively rather than silently dropped),
    appends to_move into the archive's same-named tab, rewrites the live tab
    with only to_keep, and resizes its grid down. Yields progress lines.

    The archive side is rewritten (not blindly appended to): it's recomputed as
    {rows already in the archive that are still before the new cutoff} + {rows
    just moved from live}. This makes every run self-cleaning — if the archive
    ever ends up holding rows at/after some cutoff (e.g. leftover from the very
    first full-spreadsheet duplicate, before any trimming happened), the next
    run quietly drops them instead of compounding duplicates forever."""
    live_ws = live_sh.worksheet(tab_name)
    values = live_ws.get_all_values()
    header, rows = values[0], values[1:]
    to_move, to_keep = [], []
    for r in rows:
        v = r[date_col_idx] if len(r) > date_col_idx else ""
        if v and is_before_cutoff(v):
            to_move.append(r)
        else:
            to_keep.append(r)
    yield f"{tab_name}: {len(rows)} live rows -> moving {len(to_move)} to archive, keeping {len(to_keep)} live"

    archive_ws = archive_sh.worksheet(tab_name)
    archive_values = archive_ws.get_all_values()
    archive_header = archive_values[0] if archive_values else header
    archive_kept = [
        r for r in archive_values[1:]
        if (r[date_col_idx] if len(r) > date_col_idx else "") and is_before_cutoff(r[date_col_idx])
    ]
    dropped_from_archive = len(archive_values[1:]) - len(archive_kept) if archive_values else 0
    combined = archive_kept + to_move
    if dropped_from_archive:
        yield (f"{tab_name}: archive had {dropped_from_archive} leftover row(s) at/after the new cutoff "
               f"(from an earlier full copy) — dropped, since they belong in live, not archive")
    archive_ws.clear()
    arch_all = [archive_header] + combined
    archive_ws.update(arch_all[:BATCH + 1], "A1", value_input_option="USER_ENTERED")
    for i in range(BATCH, len(combined), BATCH):
        archive_ws.append_rows(combined[i:i + BATCH], value_input_option="USER_ENTERED")
    archive_ws.resize(rows=max(len(combined) + 1, 2), cols=archive_ws.col_count)
    yield f"{tab_name}: archive now holds {len(combined)} rows ({len(archive_kept)} pre-existing + {len(to_move)} newly moved)"

    live_ws.clear()
    all_rows = [header] + to_keep
    live_ws.update(all_rows[:BATCH + 1], "A1", value_input_option="USER_ENTERED")
    for i in range(BATCH, len(to_keep), BATCH):
        live_ws.append_rows(to_keep[i:i + BATCH], value_input_option="USER_ENTERED")
    new_rc = max(len(to_keep) + 1, 2)
    live_ws.resize(rows=new_rc, cols=live_ws.col_count)
    yield f"{tab_name}: live sheet resized to {new_rc} rows x {live_ws.col_count} cols"


def run_archive(new_cutoff):
    """Generator yielding progress-log strings (safe to print directly, or stream
    to a UI). Moves rows dated before new_cutoff from the live production sheet
    into the archive spreadsheet for main_metrics, main_metrics_outbrain, and
    New_Data. Refuses to run if new_cutoff doesn't move strictly forward from the
    currently stored cutoff, since archive and live must never overlap."""
    gc = _client()
    live_sh = _production_spreadsheet(gc)
    cfg = get_archive_config(live_sh)
    if not cfg:
        yield "ERROR: _archive_config tab not found or incomplete in the production spreadsheet. Aborting."
        return

    try:
        new_d = _validate_cutoff(new_cutoff, cfg["cutoff"])
    except ValueError as e:
        yield f"ERROR: {e}"
        return
    new_cutoff_iso = new_d.isoformat()

    yield f"Current cutoff: {cfg['cutoff']}. New cutoff: {new_cutoff_iso}."
    archive_sh = gc.open_by_key(cfg["archive_sheet_id"])

    for tab in ISO_TABS:
        for line in _move_tab(live_sh, archive_sh, tab, 0, _iso_before(new_cutoff_iso)):
            yield line
    for line in _move_tab(live_sh, archive_sh, MDY_TAB, 2, _mdy_before(new_cutoff_iso)):
        yield line

    _set_cutoff(live_sh, new_cutoff_iso)
    yield f"_archive_config updated: cutoff is now {new_cutoff_iso}"
    yield "Done."


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Move old rows from the live production sheet into the archive.")
    parser.add_argument("--new-cutoff", required=True, help="New cutoff date, YYYY-MM-DD")
    args = parser.parse_args()
    for line in run_archive(args.new_cutoff):
        print(line)
