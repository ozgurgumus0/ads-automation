"""
Creative feed: Snowflake cohort data -> the creative dashboard's New_Data and
Lead_Contract_Cohort tabs (production sheet). Replaces the manual step
"the data team's Excel -> copy-paste into the sheet".

Two ways to get the data, both under Python 3.13 (Snowflake connector, SSO
login in the browser), both saving an xlsx backup in creative_feed/output/:
  - the data team's own export package, used as-is when present (NOT included in
    this repository; drop it into creative_feed/ if you have one):
    creative_feed/scripts/metrics/ + creative_feed/reports/dashboard-feed/src/export_dashboard_feed.py
  - otherwise fetchers/creative_snowflake.py, the same funnel rebuilt from the
    team's written rules and calibrated against the team's own feed files
    (within ~1%, see that file).
Feed rules either way: 2026-01-01 -> yesterday, full refresh, TikTok included,
raw 0-1 rates (6 decimals), both "Contratti Firmati" and "Contratti Unici (Lead)".

This module then writes both sheets into the live tabs, values only (RAW):
text stays text (a UTM Content starting with '=' never becomes a formula),
dates go in as date serials like the pasted ones, tab formatting is kept.
Rows are overwritten in place and only the leftover tail is cleared, so the
dashboard never reads a half-empty tab.

Refuses to write when a sheet's columns differ from the live tab's header or
it has far fewer rows than the tab (a cut-off export), unless --force.

Usage:
    python creative_feed.py                      export + write
    python creative_feed.py --dry-run            export + compare with the live tabs, write nothing
    python creative_feed.py --file X.xlsx        use an existing feed file instead of exporting
"""

import argparse
import glob
import json
import math
import os
import subprocess
import sys
from datetime import date, datetime

import gspread
import pandas as pd
from dotenv import load_dotenv

from sheets_writer import _retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

FEED_DIR = os.path.join(BASE_DIR, "creative_feed")
EXPORT_SCRIPT = os.path.join(FEED_DIR, "reports", "dashboard-feed", "src", "export_dashboard_feed.py")
OUTPUT_DIR = os.path.join(FEED_DIR, "output")
SNOWFLAKE_PYTHON = os.getenv("SNOWFLAKE_PYTHON", "py -3.13")
STATUS_PATH = os.path.join(BASE_DIR, "logs", "creative_feed_status.json")
TABS = ["New_Data", "Lead_Contract_Cohort"]
MIN_ROW_RATIO = 0.9      # fewer rows than this x the live tab = suspicious export
BATCH = 20000
EPOCH = date(1899, 12, 30)   # Google Sheets date serial 0

# Per-channel totals printed by the comparison: (channel column, value columns).
CHECK_COLS = {"New_Data": ("Canale", ["Lead Cohort", "SC1 Net Cohort", "Contratti Firmati", "Contratti Unici (Lead)"]),
              "Lead_Contract_Cohort": ("Channel", ["Contratti Totali", "Contratti Unici"])}


def package_ready():
    return os.path.exists(EXPORT_SCRIPT) and os.path.exists(os.path.join(FEED_DIR, "scripts", "metrics", "campaign_funnel.py"))


REBUILT_SCRIPT = os.path.join(BASE_DIR, "fetchers", "creative_snowflake.py")
ND_COLS = ["Canale", "UTM Campaign", "UTM Content", "Lead Created Date", "Lead Cohort", "Low Prio Cohort", "% Low Prio",
           "Called Cohort", "Reached & SC1 Scheduled", "Booking Rate (Cohort)", "SC1 Net Cohort", "% Leads to SC1 Net",
           "LOI 1", "LOI 4", "LOI 6", "LOI 7", "Contratti Firmati", "Contratti Unici (Lead)"]
LCC_COLS = ["Lead Created Date", "Contract Signed Date", "Channel", "Creative / UTM Content", "Contratti Totali", "Contratti Unici"]


def source_name():
    return "external package" if package_ready() else "rebuilt query"


def run_rebuilt():
    """The rebuilt funnel (fetchers/creative_snowflake.py) -> an xlsx shaped like the team's feed."""
    end = (date.today() - pd.Timedelta(days=1)).isoformat()   # the feed stops at yesterday
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp = os.path.join(OUTPUT_DIR, "_rebuilt.json")
    print(f"Running the rebuilt Snowflake query 2026-01-01 -> {end} (a browser login may open)...", flush=True)
    proc = subprocess.Popen(SNOWFLAKE_PYTHON.split() + ["-u", REBUILT_SCRIPT, "2026-01-01", end, tmp], cwd=BASE_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    tail = []
    for line in proc.stdout:
        if line.strip():
            tail = (tail + [line.rstrip()])[-6:]
            if not line.startswith((" ", "Traceback", "  File")) and "Warning" not in line:
                print("  " + line.rstrip(), flush=True)
    if proc.wait() != 0:
        raise RuntimeError("Snowflake query failed: " + " | ".join(tail)[-400:])
    with open(tmp, encoding="utf-8") as f:
        res = json.load(f)
    os.remove(tmp)

    nd = pd.DataFrame(res["nd"], columns=["Canale", "UTM Campaign", "UTM Content", "Lead Created Date", "Lead Cohort",
                                          "Low Prio Cohort", "Called Cohort", "Reached & SC1 Scheduled", "SC1 Net Cohort",
                                          "LOI 1", "LOI 4", "LOI 6", "LOI 7", "Contratti Firmati", "Contratti Unici (Lead)"])
    rate = lambda a, b: (a / b.where(b > 0)).fillna(0).round(6)
    nd["% Low Prio"] = rate(nd["Low Prio Cohort"], nd["Lead Cohort"])
    nd["Booking Rate (Cohort)"] = rate(nd["Reached & SC1 Scheduled"], nd["Called Cohort"])
    nd["% Leads to SC1 Net"] = rate(nd["SC1 Net Cohort"], nd["Lead Cohort"])
    nd["Lead Created Date"] = pd.to_datetime(nd["Lead Created Date"])
    nd = nd.sort_values(["Canale", "UTM Campaign", "UTM Content", "Lead Created Date"], na_position="last")[ND_COLS]

    g = pd.DataFrame(res["lcc"], columns=LCC_COLS[:4] + ["Contratti Totali", "Contratti Unici", "n"])
    lcc = g.loc[g.index.repeat(g["n"])].drop(columns="n")   # back to one row per lead
    for c in ("Lead Created Date", "Contract Signed Date"):
        lcc[c] = pd.to_datetime(lcc[c])
    lcc = lcc.sort_values(["Lead Created Date", "Channel", "Creative / UTM Content"], na_position="last")[LCC_COLS]

    out = os.path.join(OUTPUT_DIR, f"Creative_Feed_{date.today().isoformat()}_rebuilt.xlsx")
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        nd.to_excel(w, sheet_name="New_Data", index=False)
        lcc.to_excel(w, sheet_name="Lead_Contract_Cohort", index=False)
    print(f"New_Data rows: {len(nd):,} · Lead_Contract_Cohort rows: {len(lcc):,}", flush=True)
    return out


def run_export():
    """The external export under Python 3.13 if its package is here, else the rebuilt query; returns the xlsx."""
    if not package_ready():
        return run_rebuilt()
    started = datetime.now().timestamp()
    env = {**os.environ, "SNOWFLAKE_DATABASE": os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS"),
           "SNOWFLAKE_SCHEMA": os.getenv("SNOWFLAKE_SCHEMA", "RAW"), "PYTHONIOENCODING": "utf-8"}
    print("Running the external export (Snowflake, a browser login may open)...", flush=True)
    proc = subprocess.Popen(SNOWFLAKE_PYTHON.split() + ["-u", EXPORT_SCRIPT], cwd=FEED_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        if line.strip():
            print("  " + line.rstrip(), flush=True)
    if proc.wait() != 0:
        raise RuntimeError(f"export failed (exit code {proc.returncode}), see the lines above")
    files = [f for f in glob.glob(os.path.join(OUTPUT_DIR, "Creative_Feed_*.xlsx")) if os.path.getmtime(f) >= started - 5]
    if not files or os.path.getsize(max(files, key=os.path.getmtime)) == 0:
        raise RuntimeError("the export produced no file (or an empty one): the SSO login was probably not confirmed, run again")
    return max(files, key=os.path.getmtime)


def _serial(v):
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return ""
    if isinstance(v, (datetime, pd.Timestamp)):
        v = v.date()
    if isinstance(v, str):
        v = date.fromisoformat(v[:10])
    return (v - EPOCH).days


def _value(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if hasattr(v, "item"):   # numpy scalar -> python
        v = v.item()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def to_rows(df):
    date_cols = {i for i, c in enumerate(df.columns) if "Date" in c}
    return [[_serial(v) if i in date_cols else _value(v) for i, v in enumerate(r)]
            for r in df.itertuples(index=False, name=None)]


def _col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def channel_totals(df, tab):
    ch, cols = CHECK_COLS[tab]
    cols = [c for c in cols if c in df.columns]
    out = df.groupby(df[ch].fillna("").astype(str))[cols].sum(numeric_only=True)
    out.loc["TOTAL"] = out.sum()
    return out


def live_frame(ws):
    values = _retry(ws.get_all_values, value_render_option="UNFORMATTED_VALUE")
    head = values[0]
    df = pd.DataFrame([r + [""] * (len(head) - len(r)) for r in values[1:]], columns=head)
    for c in CHECK_COLS[ws.title][1]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return head, df


def compare(tab, new, live):
    print(f"\n[{tab}] rows: new {len(new):,} vs live {len(live):,}")
    a, b = channel_totals(new, tab), channel_totals(live, tab)
    t = a.join(b, how="outer", lsuffix=" new", rsuffix=" live").fillna(0)
    with pd.option_context("display.width", 250, "display.max_columns", 20):
        print(t.astype(int).to_string())


def write_tab(sh, ws, df):
    rows = to_rows(df)
    n_old, need = ws.row_count, len(rows) + 1
    if n_old < need:
        _retry(ws.add_rows, need - n_old)
    last = _col(len(df.columns))
    for i in range(0, len(rows), BATCH):
        _retry(ws.update, range_name=f"A{i + 2}", values=rows[i:i + BATCH], value_input_option="RAW")
        print(f"  {ws.title}: wrote rows {i + 2}-{i + 1 + len(rows[i:i + BATCH])}", flush=True)
    if n_old > need:   # previous run had more rows: clear the tail, then shrink the grid
        _retry(ws.batch_clear, [f"A{need + 1}:{last}{n_old}"])
        _retry(ws.resize, rows=need)
    # Date columns: keep them displayed as dates (new rows beyond the old grid have no format).
    reqs = [{"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": need,
                                      "startColumnIndex": i, "endColumnIndex": i + 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
                            "fields": "userEnteredFormat.numberFormat"}}
            for i, c in enumerate(df.columns) if "Date" in c]
    if reqs:
        _retry(sh.batch_update, {"requests": reqs})
    print(f"  {ws.title}: {len(rows):,} rows written", flush=True)


def save_status(**kw):
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    json.dump({"last_run": datetime.now().strftime("%Y-%m-%d %H:%M"), "source": source_name(), **kw},
              open(STATUS_PATH, "w"), indent=1)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Snowflake creative feed -> New_Data + Lead_Contract_Cohort")
    p.add_argument("--dry-run", action="store_true", help="compare with the live tabs, write nothing")
    p.add_argument("--file", help="use this feed xlsx instead of running the export")
    p.add_argument("--force", action="store_true", help="write even if the row-count check fails")
    a = p.parse_args()
    try:
        if not a.file:
            print(f"Source: {source_name()}", flush=True)
        path = a.file or run_export()
        print(f"Feed file: {path}", flush=True)
        frames = {t: pd.read_excel(path, sheet_name=t) for t in TABS}
        sh = gspread.service_account(filename=os.path.join(BASE_DIR, os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"))).open_by_key(
            os.getenv("GOOGLE_SHEETS_ID_PRODUCTION"))
        problems, live = [], {}
        for t in TABS:
            ws = sh.worksheet(t)
            head, ldf = live_frame(ws)
            live[t] = ws
            compare(t, frames[t], ldf)
            if list(frames[t].columns) != head:
                problems.append(f"{t}: columns differ from the live tab.\n    feed: {list(frames[t].columns)}\n    live: {head}")
            if len(frames[t]) < MIN_ROW_RATIO * len(ldf) and not a.force:
                problems.append(f"{t}: only {len(frames[t]):,} rows vs {len(ldf):,} live (looks cut off; --force to write anyway)")
        counts = {t: len(frames[t]) for t in TABS}
        if problems:
            print("\nNOT WRITTEN:\n  " + "\n  ".join(problems), flush=True)
            save_status(status="error", file=os.path.basename(path), rows=counts, message="; ".join(x.split("\n")[0] for x in problems))
            sys.exit(1)
        if a.dry_run:
            print("\nDry run: nothing written.", flush=True)
            save_status(status="dry-run", file=os.path.basename(path), rows=counts, message="compared, not written")
            return
        print("\nWriting the live tabs...", flush=True)
        for t in TABS:
            write_tab(sh, live[t], frames[t])
        save_status(status="ok", file=os.path.basename(path), rows=counts, message="")
        print("Done.", flush=True)
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        save_status(status="error", message=str(e)[:400])
        sys.exit(1)


if __name__ == "__main__":
    main()
