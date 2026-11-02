"""
Writes fetchers/seo.py's tables into the SEO sheet (SEO_SHEETS_ID).

Each source runs on its own: one failing (e.g. Search Console before its
access is granted) is logged in `_status` and leaves its tabs as they were;
the others still update. Rows older than the refetched window are kept, so
history outlives a source's retention limit.

Tabs owned by people (README is rewritten, `priorities` is NEVER overwritten
once it exists) sit next to the data tabs.
"""

import os
from datetime import date, datetime, timedelta

import gspread

from fetchers import seo
from sheets_writer import _retry, SERVICE_ACCOUNT_FILE

STATUS_COLUMNS = ["source", "status", "last_run", "rows", "first_date", "last_date", "message"]


def _open(sheet_id):
    sa_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SERVICE_ACCOUNT_FILE)
    return gspread.service_account(filename=sa_path).open_by_key(sheet_id)


def _worksheet(sh, name, cols):
    try:
        return sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        return _retry(sh.add_worksheet, title=name, rows=100, cols=max(cols, 2))


def _read(sh, name):
    """Existing rows of a tab as dicts (empty list if the tab doesn't exist)."""
    try:
        # Unformatted: formatted text would follow the sheet's locale ("0,2344")
        # and round to the display pattern, corrupting numbers on every merge.
        values = _retry(sh.worksheet(name).get_all_values, value_render_option="UNFORMATTED_VALUE")
    except gspread.exceptions.WorksheetNotFound:
        return []
    if len(values) < 2:
        return []
    head = values[0]
    return [dict(zip(head, r)) for r in values[1:] if any(r)]


def write(sh, name, columns, rows, numeric=()):
    """Replace a tab with `rows` (header + values, RAW so ISO dates stay text)."""
    ws = _worksheet(sh, name, len(columns))
    data = [list(columns)] + [[_cell(r.get(c, ""), c in numeric) for c in columns] for r in rows]
    _retry(ws.clear)
    _retry(ws.resize, rows=max(len(data) + 1, 2), cols=len(columns))
    for i in range(0, len(data), 20000):
        _retry(ws.update, f"A{i + 1}", data[i:i + 20000], value_input_option="RAW")
    reqs = [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": len(data),
                  "startColumnIndex": list(columns).index(c), "endColumnIndex": list(columns).index(c) + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.####"}}},
        "fields": "userEnteredFormat.numberFormat"}} for c in numeric]
    reqs.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                "fields": "userEnteredFormat.textFormat.bold"}})
    reqs.append({"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                           "fields": "gridProperties.frozenRowCount"}})
    _retry(sh.batch_update, {"requests": reqs})


def _cell(v, numeric):
    if v is None or v == "":
        return ""
    if numeric:
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return ""
    return v


def _merge(existing, new, date_col, start):
    """Keep existing rows dated before `start`, then everything freshly fetched."""
    keep = [r for r in existing if str(r.get(date_col, "")) < start]
    return keep + new


def _span(rows, col):
    ds = sorted(str(r[col]) for r in rows if r.get(col))
    return (ds[0], ds[-1]) if ds else ("", "")


def sync(sheet_id, only=None, full=False):
    sh = _open(sheet_id)
    status = {r["source"]: r for r in _read(sh, "_status")}
    # Up to today: today's numbers are partial, but every run refetches the last
    # REFETCH_DAYS days, so the next run completes them.
    today = date.today().isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def run(source, tabs, fetch, floor=seo.HISTORY_START, end=today):
        """tabs: tab names whose rows `fetch(start, end)` returns, in order."""
        if only and source not in only:
            return
        existing = {t: _read(sh, t) for t in tabs}
        date_col = seo.TABLES[tabs[0]][2]
        last = max((str(r.get(date_col, "")) for r in existing[tabs[0]]), default="")
        start = floor if full else seo.fetch_start(last, floor)
        if date_col == "month":   # refetch whole months only
            start = start[:8] + "01"
        print(f"[ SEO/{source} ] fetching {start} -> {end} ...")
        try:
            results = fetch(start, end)
            if len(tabs) == 1:
                results = (results,)
            spans, total = [], 0
            for tab, rows in zip(tabs, results):
                cols, numeric, dcol = seo.TABLES[tab]
                if isinstance(rows, Complete):
                    merged = list(rows)
                else:
                    merged = _merge(existing[tab], rows, dcol, start[:7] if dcol == "month" else start)
                merged.sort(key=lambda r: str(r.get(dcol, "")))
                write(sh, tab, cols, merged, numeric)
                total += len(merged)
                spans.append(_span(merged, dcol))
                print(f"[ SEO/{source} ] {tab}: {len(rows)} fetched, {len(merged)} rows in tab")
            status[source] = {"source": source, "status": "ok", "last_run": now, "rows": total,
                              "first_date": spans[0][0], "last_date": spans[0][1], "message": ""}
        except Exception as e:  # keep the tabs as they were, record why
            print(f"[ SEO/{source} ] ERROR: {e}")
            prev = status.get(source, {})
            status[source] = {**{k: prev.get(k, "") for k in STATUS_COLUMNS}, "source": source,
                              "status": "error", "last_run": now, "message": str(e)[:450]}

    ga4 = None

    def ga4_s():
        nonlocal ga4
        ga4 = ga4 or seo.ga4_session()
        return ga4

    run("ga4_sections", ["ga4_daily_section"], lambda s, e: seo.fetch_ga4_sections(ga4_s(), s, e))
    run("ga4_product_choice", ["ga4_daily_product_choice"], lambda s, e: seo.fetch_ga4_product_choice(ga4_s(), s, e))
    run("ga4_product_leads", ["ga4_daily_product_leads"], lambda s, e: seo.fetch_ga4_product_leads(ga4_s(), s, e))
    run("ga4_channels", ["ga4_monthly_channels"], lambda s, e: seo.fetch_ga4_channels(ga4_s(), s, e))
    run("ga4_monthly_sessions", ["ga4_monthly_sessions"], lambda s, e: seo.fetch_ga4_monthly_sessions(ga4_s(), s, e))
    # Search Console keeps ~16 months. Fresh data up to yesterday, so its windows
    # match GA4's and Snowflake's; the last days are provisional until a later run
    # refetches them as final.
    gsc_floor = max(seo.HISTORY_START, (date.today() - timedelta(days=480)).isoformat())
    run("gsc", ["gsc_daily_section", "gsc_daily_totals"], seo.fetch_gsc, floor=gsc_floor,
        end=(date.today() - timedelta(days=1)).isoformat())
    run("snowflake_leads", ["sf_daily_leads"], seo.fetch_sf_leads)
    run("crm_organic", ["crm_daily_organic"], seo.fetch_crm_organic, floor="2026-01-01")
    run("peec", ["peec_daily_brand", "peec_daily_engine", "peec_snapshot_sources", "peec_snapshot_branded"],
        lambda s, e: _peec_with_snapshots(sh, s, e), floor="2026-06-01")

    write(sh, "_status", STATUS_COLUMNS, sorted(status.values(), key=lambda r: r["source"]), ["rows"])
    write_static(sh)
    print("[ SEO ] Done.")


class Complete(list):
    """A fetch result that already holds the whole tab (written as-is, not merged)."""


def _peec_with_snapshots(sh, start, end):
    """Daily Peec tables merge by date like the others. Snapshot tabs keep every
    earlier snapshot and replace only one already taken for the same day."""
    brand, engine, sources, branded = seo.fetch_peec(start, end)
    old_src = [r for r in _read(sh, "peec_snapshot_sources") if r.get("snapshot_date") != end]
    old_br = [r for r in _read(sh, "peec_snapshot_branded") if r.get("snapshot_date") != end]
    return brand, engine, Complete(old_src + sources), Complete(old_br + branded)


# ---------------------------------------------------------------------------
# Human tabs
# ---------------------------------------------------------------------------

README = [
    ["SEO & GEO data — the company", ""],
    ["Filled automatically every morning by ads_automation (python main.py --source seo). Do not edit the data tabs: the next run overwrites them. Edit only 'priorities'.", ""],
    ["", ""],
    ["Tab", "What it holds"],
    ["_status", "Per source: last run, ok/error, date range held, error message. Check here first if something looks stale."],
    ["sections", "The 10 page-based sections (+ 'Altro /it') and the URL rule that defines each."],
    ["priorities", "The 3 priorities of the week shown on the dashboard. EDIT THIS TAB (n, priority, owner, due, status). Never overwritten."],
    ["ga4_daily_section", "GA4, organic sessions landing on /it (example.org excluded), per day and section: sessions, sessions that reached slider step 1, lead sessions (slider-success). From 2025-01-01."],
    ["ga4_daily_product_choice", "GA4, per day and section: sessions that answered the slider's product question with FV / PdC / Combi / Sostituzione = product INTENT at the first question."],
    ["ga4_daily_product_leads", "GA4 funnel, per day: organic leads by the product chosen in the slider (users, all sections together). Per-section product split of LEADS needs Snowflake."],
    ["ga4_monthly_channels", "GA4, monthly sessions per channel for the whole www.example.com host (channel mix). From 2025-01."],
    ["gsc_daily_section", "Search Console, per day and section: clicks, impressions, position x impressions (divide by impressions for the average position). Page-based."],
    ["gsc_daily_totals", "Search Console, per day for all /it pages: lens all / brand / nonbrand (brand regex from BRAND_REGEX). Brand + nonbrand < all: Google hides anonymised queries."],
    ["sf_daily_leads", "THE lead number. Snowflake, per day, section (by entranceUrl) and product chosen in the slider (FV / PdC / Combi): leads with channel 'organic', example.org excluded (the SEO lead's rule). /it/finalmente-indipendenti counts as Homepage; section 'n/d' = entrance page not recorded (counted in the total only). From 2025-01-01, but the 'organic' label jumps ~150 -> ~650 leads/month in Oct 2025 (labelling change): the dashboard compares leads only from 2025-10-01."],
    ["crm_daily_organic", "CRM leads labelled 'Organic' (creative dashboard sheet, New_Data). Wider than SEO: also example.org, direct and some paid clicks. From 2026-01-01 (no earlier data in that sheet)."],
    ["peec_daily_brand / peec_daily_engine", "Peec AI, per day: visibility, share of voice, sentiment, position per brand, and our brand per AI engine. From 2026-06-03 (start of the Peec project)."],
    ["peec_snapshot_sources / peec_snapshot_branded", "Peec AI, last 30 days at each run: most-cited domains (top) and domains citing competitors but not us (gap); our visibility on branded vs non-branded prompts."],
    ["seed_report_*", "Frozen numbers from the SEO lead's SEO_Performance_Report_v2 (data as of 11/10/26). Only the monthly Trend's GSC rows are still used (year-on-year fallback); leads now come from sf_daily_leads."],
    ["", ""],
    ["Definitions", ""],
    ["Lead", "Snowflake organic lead (sf_daily_leads). Every lead figure on the dashboard uses it."],
    ["CR", "Snowflake leads / GA4 organic sessions."],
    ["Lead (GA4)", "Session with the Formslider 'slider-success' event: used only inside the slider funnel (start/end rate). GA4 sees ~60% of the Snowflake leads (cookie consent / ad-blockers)."],
    ["Start", "Session reaching slider step 1: event_value 1 / 'Step 1', or the product question answered at value 2."],
    ["Windows", "The dashboard cuts 7 and 28 days ending on the last complete Sunday (Monday-Sunday weeks, like the report), vs the same length just before."],
]


def write_static(sh):
    write(sh, "sections", ["section", "rule", "description"],
          [{"section": n, "rule": rx, "description": d} for n, rx, d in seo.SECTIONS] +
          [{"section": seo.OTHER_SECTION, "rule": "any other /it page", "description": "not in a section"}])
    ws = _worksheet(sh, "README", 2)
    _retry(ws.clear)
    _retry(ws.update, "A1", README, value_input_option="RAW")
    try:
        sh.worksheet("priorities")
    except gspread.exceptions.WorksheetNotFound:
        write(sh, "priorities", ["n", "priority", "owner", "due", "status"],
              [{"n": i} for i in (1, 2, 3)], ["n"])
