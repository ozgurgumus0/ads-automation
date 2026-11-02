"""
SEO & GEO fetcher (GA4 + Search Console + Snowflake leads + Peec + CRM organic) -> the SEO sheet.

Feeds the SEO & GEO dashboard (an automated version of a manual weekly SEO
report). Same rules as that report's pipeline; calibrated against it.

Everything is stored DAILY so the dashboard can cut any window (7/28 days, the
previous period, months, year-on-year) itself. History starts at HISTORY_START
where the source has it:
  GA4    -> from 2025-01-01 (property data starts 2024-09)
  GSC    -> from 2025-01-01, but Search Console only keeps ~16 months
  Leads  -> Snowflake, from 2025-01-01 (the report's lead number)
  Peec   -> from the project's first day (2026-06-03)
  CRM    -> from 2026-01-01 (the creative dashboard's New_Data starts there)

Incremental: a run refetches the last REFETCH_DAYS days and keeps every older
row already in the tab, so history survives a source's retention limit. An
empty tab is backfilled from HISTORY_START.

Sections = the report's 10 page-based buckets (by GA4 landing page / GSC page)
plus "Altro /it" for the remaining /it pages, so section totals add up to all
of /it. Brand is a QUERY lens in GSC (gsc_daily_totals), never a section: its
clicks already sit inside the pages they land on.

Counts only, nothing per user.
"""

import json
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import date, timedelta

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

from fetchers.whatsapp import _session as ga4_session, _run_report, _exact

HISTORY_START = "2025-01-01"
REFETCH_DAYS = 40
GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "")
# Site / brand configuration (all overridable from .env)
GSC_SITE = os.getenv("GSC_SITE", "sc-domain:example.com")
SITE_HOST = os.getenv("SEO_SITE_HOST", "www.example.com")          # main site host in GA4
SITE_DOMAIN = os.getenv("SEO_SITE_DOMAIN", "example.com")          # domain lead entrance URLs must end with
EXCLUDED_HOST = os.getenv("SEO_EXCLUDED_HOST", "example.org")      # separate site left out of the organic scope
BRAND_RE = os.getenv("SEO_BRAND_REGEX", "yourbrand|your brand")    # brand-query regex (include common misspellings)
# The brand_report lens excludes recruiting queries
CAREERS_RE = os.getenv("SEO_CAREERS_REGEX", "career|careers|lavora|job|jobs|hiring|stage|tirocinio|recruit")
CRM_SHEET_ID = os.getenv("GOOGLE_SHEETS_ID_PRODUCTION", "")   # creative dashboard sheet
PEEC_MCP_URL = "https://api.peec.ai/mcp"

# (section, rule on the lowercased /it path without trailing slash, description)
SECTIONS = [
    ("Homepage",          r"^/it$",                             "<site>/it"),
    ("Prodotto FV",       r"^/it/fotovoltaico$",                "product page"),
    ("Prodotto PdC",      r"^/it/pompa-di-calore$",             "product page"),
    ("Prodotto Batteria", r"^/it/stoccaggio-della-batteria$",   "product page"),
    ("Prodotto Wallbox",  r"^/it/wallbox$",                     "product page"),
    ("Articoli FV",       r"^/it/fotovoltaico/",                "articles under /it/fotovoltaico/"),
    ("Articoli PdC",      r"^/it/pompa-di-calore/",             "articles under /it/pompa-di-calore/"),
    ("Articoli Batterie", r"^/it/batterie/",                    "articles under /it/batterie/"),
    ("Articoli Wallbox",  r"^/it/wallbox/",                     "articles under /it/wallbox/"),
    ("FV Local",          r"^/it/fotovoltaico-local(/|$)",      "local landing pages"),
]
OTHER_SECTION = "Altro /it"
_SECTION_RX = [(n, re.compile(rx)) for n, rx, _ in SECTIONS]

# Slider product answers (customEvent:event_label on the product question).
PRODUCTS = {"fotovoltaico": "FV", "photovoltaic": "FV", "pompa": "PdC", "heatpump": "PdC",
            "fv_pompa": "Combi", "sostituzione": "Sostituzione"}


def section_of(path):
    """Section for a /it path, None for anything outside /it."""
    p = (path or "").lower().split("?")[0].split("#")[0].rstrip("/")
    if not re.match(r"^/it($|/)", p):
        return None
    for name, rx in _SECTION_RX:
        if rx.search(p):
            return name
    return OTHER_SECTION


# Every tab this fetcher owns: name -> (columns, numeric columns, date column).
TABLES = {
    "ga4_daily_section": (["date", "section", "sessions", "start_sessions", "lead_sessions"],
                          ["sessions", "start_sessions", "lead_sessions"], "date"),
    "ga4_daily_product_choice": (["date", "section", "product", "sessions"], ["sessions"], "date"),
    "ga4_daily_product_leads": (["date", "product", "lead_users"], ["lead_users"], "date"),
    "ga4_monthly_channels": (["month", "channel", "sessions"], ["sessions"], "month"),
    "ga4_monthly_sessions": (["month", "sessions"], ["sessions"], "month"),
    "gsc_daily_section": (["date", "section", "clicks", "impressions", "position_x_impressions"],
                          ["clicks", "impressions", "position_x_impressions"], "date"),
    "gsc_daily_totals": (["date", "lens", "clicks", "impressions", "position_x_impressions"],
                         ["clicks", "impressions", "position_x_impressions"], "date"),
    "sf_daily_leads": (["date", "section", "product", "leads"], ["leads"], "date"),
    "crm_daily_organic": (["date", "organic_leads"], ["organic_leads"], "date"),
    # answers = AI answers Peec collected that day (visibility's denominator):
    # a jump means the prompt set changed and days either side aren't comparable.
    "peec_daily_brand": (["date", "brand", "is_own", "visibility", "share_of_voice", "sentiment",
                          "position", "mentions", "answers"],
                         ["is_own", "visibility", "share_of_voice", "sentiment", "position", "mentions", "answers"],
                         "date"),
    "peec_daily_engine": (["date", "engine", "visibility", "share_of_voice", "sentiment", "position"],
                          ["visibility", "share_of_voice", "sentiment", "position"], "date"),
    "peec_snapshot_sources": (["snapshot_date", "period_start", "period_end", "kind", "domain",
                               "classification", "citation_count", "retrieval_count",
                               "retrieved_percentage", "competitors"],
                              ["citation_count", "retrieval_count", "retrieved_percentage", "competitors"],
                              "snapshot_date"),
    "peec_snapshot_branded": (["snapshot_date", "period_start", "period_end", "segment", "n_prompts",
                               "visibility_brand"], ["n_prompts", "visibility_brand"], "snapshot_date"),
}


# ---------------------------------------------------------------------------
# GA4
# ---------------------------------------------------------------------------

def _f(field, value, match="EXACT"):
    return {"filter": {"fieldName": field, "stringFilter": {"value": value, "matchType": match}}}


def _and(*exprs):
    return {"andGroup": {"expressions": list(exprs)}}


def _or(*exprs):
    return {"orGroup": {"expressions": list(exprs)}}


# Organic sessions on www.example.com and the other example.com hosts; excludes the
# separate example.org Webflow site (the report's scope).
_ORGANIC = [_f("sessionDefaultChannelGroup", "Organic Search"),
            {"notExpression": _f("hostName", EXCLUDED_HOST, "CONTAINS")}]
_SLIDER = _f("customEvent:event_category", "Formslider")
_LEAD = _f("customEvent:event_action", "slider-success")
# First step, value-based so it covers both eras of the first question
# (CAP-first, then product-first): event_value 1 / "Step 1", or the product
# question answered at value 2.
_START = _or(_f("customEvent:event_value", "1"), _f("customEvent:event_value", "Step 1"),
             _and(_f("customEvent:event_action", "Per quale prodotto", "BEGINS_WITH"),
                  _f("customEvent:event_value", "2")),
             _and(_f("customEvent:event_action", "A quale prodotto", "BEGINS_WITH"),
                  _f("customEvent:event_value", "2")))
_PRODUCT_Q = _or(_f("customEvent:event_action", "Per quale prodotto", "BEGINS_WITH"),
                 _f("customEvent:event_action", "A quale prodotto", "BEGINS_WITH"))


def _iso(ga4_date):
    return f"{ga4_date[:4]}-{ga4_date[4:6]}-{ga4_date[6:8]}"


def _ga4_by_section(session, extra, start, end):
    """{(date, section): sessions} for organic /it sessions matching `extra`."""
    rows = _run_report(session, ["date", "landingPage"], "sessions",
                       _and(*_ORGANIC, *extra), start, end)
    out = defaultdict(float)
    for dims, v in rows:
        sec = section_of(dims["landingPage"])
        if sec:
            out[(_iso(dims["date"]), sec)] += v
    return out


def fetch_ga4_sections(session, start, end):
    sess = _ga4_by_section(session, [], start, end)
    starts = _ga4_by_section(session, [_SLIDER, _START], start, end)
    leads = _ga4_by_section(session, [_SLIDER, _LEAD], start, end)
    keys = sorted(set(sess) | set(starts) | set(leads))
    return [{"date": d, "section": s, "sessions": int(sess.get((d, s), 0)),
             "start_sessions": int(starts.get((d, s), 0)), "lead_sessions": int(leads.get((d, s), 0))}
            for d, s in keys]


def fetch_ga4_product_choice(session, start, end):
    """Per section, sessions answering the slider's product question with each product.
    This is INTENT at the first question, not the product of the leads (GA4 can't
    join the two events per landing page)."""
    rows = _run_report(session, ["date", "landingPage", "customEvent:event_label"], "sessions",
                       _and(*_ORGANIC, _SLIDER, _PRODUCT_Q), start, end)
    out = defaultdict(float)
    for dims, v in rows:
        sec = section_of(dims["landingPage"])
        prod = PRODUCTS.get(dims["customEvent:event_label"].strip().lower())
        if sec and prod:
            out[(_iso(dims["date"]), sec, prod)] += v
    return [{"date": d, "section": s, "product": p, "sessions": int(v)}
            for (d, s, p), v in sorted(out.items())]


def fetch_ga4_product_leads(session, start, end):
    """Organic leads split by the product chosen in the slider, one funnel report
    per day (product answer -> slider-success, broken down by answer). GA4 funnels
    count USERS, so totals sit slightly below lead sessions."""
    url = f"https://analyticsdata.googleapis.com/v1alpha/properties/{GA4_PROPERTY_ID}:runFunnelReport"

    def ff(field, value, match="EXACT"):
        return {"funnelFieldFilter": {"fieldName": field,
                                      "stringFilter": {"value": value, "matchType": match}}}
    step1 = {"andGroup": {"expressions": [
        ff("sessionDefaultChannelGroup", "Organic Search"), ff("hostName", SITE_HOST),
        {"orGroup": {"expressions": [ff("customEvent:event_action", "Per quale prodotto", "BEGINS_WITH"),
                                     ff("customEvent:event_action", "A quale prodotto", "BEGINS_WITH")]}}]}}
    out = []
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        body = {"dateRanges": [{"startDate": d.isoformat(), "endDate": d.isoformat()}],
                "funnel": {"steps": [{"name": "product", "filterExpression": step1},
                                     {"name": "lead", "filterExpression": ff("customEvent:event_action", "slider-success")}]},
                "funnelBreakdown": {"breakdownDimension": {"name": "customEvent:event_label"}, "limit": 15}}
        # 429/5xx are transient on Google's side (a single 502 used to abort the
        # whole 40-day run): retry with backoff before giving up.
        for wait in (2, 5, 15, 30, None):
            r = session.post(url, json=body)
            if r.status_code != 429 and r.status_code < 500 or wait is None:
                break
            time.sleep(wait)
        if r.status_code != 200:
            raise RuntimeError(f"GA4 funnel error {r.status_code} on {d}: {r.text[:200]}")
        agg = defaultdict(int)
        for row in r.json().get("funnelTable", {}).get("rows", []):
            step, label = [v.get("value", "") for v in row["dimensionValues"]][:2]
            prod = PRODUCTS.get(label.strip().lower())
            if step.startswith("2.") and prod:
                agg[prod] += int(float(row["metricValues"][0]["value"]))
        out += [{"date": d.isoformat(), "product": p, "lead_users": n} for p, n in sorted(agg.items())]
        d += timedelta(days=1)
    return out


def fetch_ga4_monthly_sessions(session, start, end):
    """the SEO lead's Trend "Organic Sessions": Organic Search AND landingPage contains /it, one total
    per month. GA4 counts fewer sessions for a whole month than the
    sum of its days or landing pages, so it is pulled separately."""
    expr = _and(_f("sessionDefaultChannelGroup", "Organic Search"), _f("landingPage", "/it", "CONTAINS"))
    rows = _run_report(session, ["yearMonth"], "sessions", expr, start, end)
    return [{"month": f"{d['yearMonth'][:4]}-{d['yearMonth'][4:]}", "sessions": int(v)}
            for d, v in sorted(rows, key=lambda x: x[0]["yearMonth"])]


def fetch_ga4_channels(session, start, end):
    """Monthly session share by channel for the whole www.example.com host (the
    report's Mix_Canali_GA4 definition)."""
    rows = _run_report(session, ["yearMonth", "sessionDefaultChannelGroup"], "sessions",
                       _exact("hostName", SITE_HOST), start, end)
    return [{"month": f"{d['yearMonth'][:4]}-{d['yearMonth'][4:]}", "channel": d["sessionDefaultChannelGroup"],
             "sessions": int(v)} for d, v in sorted(rows, key=lambda x: (x[0]["yearMonth"], -x[1]))]


# ---------------------------------------------------------------------------
# Search Console (the ads-automation service account must be a user on
# sc-domain:example.com and the Search Console API enabled in its project)
# ---------------------------------------------------------------------------

def _gsc_session():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"))
    creds = service_account.Credentials.from_service_account_file(
        path, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    return AuthorizedSession(creds)


def _gsc_query(session, body):
    url = ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
           f"{requests.utils.quote(GSC_SITE, safe='')}/searchAnalytics/query")
    out, start_row = [], 0
    while True:
        r = session.post(url, json={**body, "rowLimit": 25000, "startRow": start_row, "dataState": "all"})
        if r.status_code != 200:
            raise RuntimeError(f"Search Console error {r.status_code}: {r.text[:300]}")
        page = r.json().get("rows", [])
        out += page
        if len(page) < 25000:
            return out
        start_row += 25000


def _month_chunks(start, end):
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        nxt = min((d.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1), last)
        yield d.isoformat(), nxt.isoformat()
        d = nxt + timedelta(days=1)


def _day_chunks(start, end, days):
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        nxt = min(d + timedelta(days=days - 1), last)
        yield d.isoformat(), nxt.isoformat()
        d = nxt + timedelta(days=1)


def fetch_gsc(start, end):
    session = _gsc_session()
    page_it = {"dimension": "page", "operator": "contains", "expression": SITE_DOMAIN + "/it"}
    sec = defaultdict(lambda: [0.0, 0.0, 0.0])
    for s, e in _month_chunks(start, end):
        for r in _gsc_query(session, {"startDate": s, "endDate": e, "type": "web",
                                      "dimensions": ["date", "page"],
                                      "dimensionFilterGroups": [{"filters": [page_it]}]}):
            d, url = r["keys"]
            path = re.sub(r"^https?://[^/]+", "", url)
            name = section_of(path)
            if name:
                a = sec[(d, name)]
                a[0] += r["clicks"]; a[1] += r["impressions"]; a[2] += r["position"] * r["impressions"]
    sections = [{"date": d, "section": n, "clicks": int(a[0]), "impressions": int(a[1]),
                 "position_x_impressions": round(a[2], 1)} for (d, n), a in sorted(sec.items())]

    # Query lenses over all /it pages. Brand/non-brand rows leave out the
    # queries Google anonymises, so brand + non-brand < all.
    # "brand_report" = the SEO lead's Brand row (the manual report's definition): brand queries on the whole site,
    # recruiting / German-market queries excluded, no page filter.
    lenses = {"all": [page_it],
              "brand": [page_it, {"dimension": "query", "operator": "includingRegex", "expression": BRAND_RE}],
              "nonbrand": [page_it, {"dimension": "query", "operator": "excludingRegex", "expression": BRAND_RE}],
              "brand_report": [{"dimension": "query", "operator": "includingRegex", "expression": BRAND_RE},
                               {"dimension": "query", "operator": "excludingRegex", "expression": CAREERS_RE}]}
    totals = []
    for lens, filters in lenses.items():
        for r in _gsc_query(session, {"startDate": start, "endDate": end, "type": "web", "dimensions": ["date"],
                                      "dimensionFilterGroups": [{"filters": filters}]}):
            totals.append({"date": r["keys"][0], "lens": lens, "clicks": int(r["clicks"]),
                           "impressions": int(r["impressions"]),
                           "position_x_impressions": round(r["position"] * r["impressions"], 1)})
    totals.sort(key=lambda x: (x["date"], x["lens"]))
    return sections, totals


# ---------------------------------------------------------------------------
# Snowflake organic leads = THE lead number of the report (the SEO lead's rule:
# channel 'organic', example.org excluded, section by entranceUrl). The query runs
# in fetchers/seo_snowflake.py under Python 3.13 (connector not on 3.14).
# ---------------------------------------------------------------------------

SNOWFLAKE_PYTHON = os.getenv("SNOWFLAKE_PYTHON", "py -3.13")
# Campaign landing pages the SEO lead's bucketing counts as Homepage (leads only).
LEAD_HOMEPAGE_ALIASES = {"/it/finalmente-indipendenti"}
SF_PRODUCTS = {"photovoltaic": "FV", "heatpump": "PdC", "photovoltaic + heatpump": "Combi"}
NO_PAGE = "n/d"   # entrance page not recorded (about:srcdoc, blank): counted in the total only


def lead_section_of(url):
    """Section for a Snowflake entranceUrl given as lowercased host/path."""
    host, _, path = (url or "").partition("/")
    if not host.endswith(SITE_DOMAIN):
        return NO_PAGE
    path = "/" + path.rstrip("/")
    if path in LEAD_HOMEPAGE_ALIASES:
        return "Homepage"
    return section_of(path) or NO_PAGE


def fetch_sf_leads(start, end):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seo_snowflake.py")
    r = subprocess.run(SNOWFLAKE_PYTHON.split() + [script, start, end], capture_output=True, text=True,
                       timeout=600, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"Snowflake: {(r.stderr or r.stdout).strip()[-400:]}")
    agg = defaultdict(int)
    for d, url, prod, n in json.loads(r.stdout.strip().splitlines()[-1]):
        agg[(d, lead_section_of(url), SF_PRODUCTS.get((prod or "").strip().lower(), "Altro"))] += n
    return [{"date": d, "section": s, "product": p, "leads": n} for (d, s, p), n in sorted(agg.items())]


# ---------------------------------------------------------------------------
# CRM organic leads (creative dashboard sheet, CRM channel label "Organic").
# Wider than SEO: also example.org, direct and some paid-click leads.
# ---------------------------------------------------------------------------

def fetch_crm_organic(start, end):
    tq = (f"select D, sum(E) where A = 'Organic' and D >= date '{start}' and D <= date '{end}' "
          "group by D order by D")
    url = (f"https://docs.google.com/spreadsheets/d/{CRM_SHEET_ID}/gviz/tq?tqx=out:json&headers=1"
           f"&sheet=New_Data&tq={requests.utils.quote(tq)}")
    txt = requests.get(url, timeout=120).text
    j = json.loads(re.search(r"setResponse\((.*)\);?\s*$", txt, re.S).group(1))
    if j.get("status") != "ok":
        raise RuntimeError(f"creative sheet query failed: {j.get('errors')}")
    out = []
    for row in j["table"]["rows"]:
        y, m, d = map(int, re.findall(r"\d+", row["c"][0]["v"]))
        out.append({"date": date(y, m + 1, d).isoformat(), "organic_leads": int(row["c"][1]["v"] or 0)})
    return out


# ---------------------------------------------------------------------------
# Peec AI (Personal Access Token works only on the MCP endpoint)
# ---------------------------------------------------------------------------

class _Peec:
    def __init__(self, token):
        self.token, self.sid, self.n = token, None, 1
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "ads-automation-seo", "version": "1.0"}}})
        if r.status_code >= 400:
            raise RuntimeError(f"Peec initialize -> HTTP {r.status_code}: {r.text[:200]}")
        self.sid = r.headers.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, body):
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        return requests.post(PEEC_MCP_URL, headers=h, json=body, timeout=180)

    def table(self, name, args):
        self.n += 1
        r = self._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        text = r.content.decode("utf-8", "replace")
        msgs = ([json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:")]
                if "event-stream" in r.headers.get("content-type", "") else [json.loads(text)])
        for m in msgs:
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"Peec {name}: {m['error']}")
                text = m["result"]["content"][0]["text"]
                if m["result"].get("isError"):
                    raise RuntimeError(f"Peec {name}: {text[:300]}")
                data = json.loads(text)
                return [dict(zip(data["columns"], row)) for row in data.get("rows", [])]
        return []


def fetch_peec(start, end, snapshot_days=30):
    token = os.getenv("PEEC_PAT")
    if not token:
        raise RuntimeError("PEEC_PAT is not set in .env")
    pc = _Peec(token)
    pid = pc.table("list_projects", {})[0]["id"]
    brands = pc.table("list_brands", {"project_id": pid})
    own = next(b["id"] for b in brands if b.get("is_own"))
    is_own = {b["id"]: int(bool(b.get("is_own"))) for b in brands}
    engines = {c["id"]: c.get("description") or c["id"] for c in pc.table("list_model_channels", {"project_id": pid})}

    daily_brand, daily_engine = [], []
    # Peec caps a report at 1000 rows: 15-day chunks keep brands x days under it,
    # and the per-engine report is restricted to the own brand.
    for s, e in _day_chunks(start, end, 15):
        base = {"project_id": pid, "start_date": s, "end_date": e, "limit": 1000}
        for r in pc.table("get_brand_report", {**base, "dimensions": ["date"]}):
            daily_brand.append({"date": r["date"], "brand": r["brand_name"], "is_own": is_own.get(r["brand_id"], 0),
                                "visibility": r.get("visibility"), "share_of_voice": r.get("share_of_voice"),
                                "sentiment": r.get("sentiment"), "position": r.get("position"),
                                "mentions": r.get("mention_count"), "answers": r.get("visibility_total")})
        for r in pc.table("get_brand_report", {**base, "dimensions": ["date", "model_channel_id"],
                                                "filters": [{"field": "brand_id", "operator": "in", "values": [own]}]}):
            if r.get("brand_id") == own:
                daily_engine.append({"date": r["date"], "engine": engines.get(r["model_channel_id"], r["model_channel_id"]),
                                     "visibility": r.get("visibility"), "share_of_voice": r.get("share_of_voice"),
                                     "sentiment": r.get("sentiment"), "position": r.get("position")})
    daily_brand.sort(key=lambda x: (x["date"], -(x["visibility"] or 0)))
    daily_engine.sort(key=lambda x: (x["date"], x["engine"]))

    # Rolling snapshot over the last `snapshot_days` (domains and the branded
    # split have no daily dimension): one dated block per run.
    ps = (date.fromisoformat(end) - timedelta(days=snapshot_days - 1)).isoformat()
    base = {"project_id": pid, "start_date": ps, "end_date": end}
    snap = {"snapshot_date": end, "period_start": ps, "period_end": end}
    sources = []
    for kind, extra in (("top", {}), ("gap", {"filters": [{"field": "gap", "operator": "gt", "value": 0}]})):
        for r in pc.table("get_domain_report", {**base, **extra, "limit": 15,
                                                "order_by": [{"field": "citation_count", "direction": "desc"}]}):
            sources.append({**snap, "kind": kind, "domain": r.get("domain"), "classification": r.get("classification"),
                            "citation_count": r.get("citation_count"), "retrieval_count": r.get("retrieval_count"),
                            "retrieved_percentage": r.get("retrieved_percentage"),
                            "competitors": len(r.get("mentioned_brand_ids") or [])})
    prompts = pc.table("list_prompts", {"project_id": pid})
    brand_rx = re.compile(BRAND_RE, re.I)
    groups = {"tutti": None,
              "branded": [p["id"] for p in prompts if brand_rx.search(p.get("text") or "")],
              "non-branded": [p["id"] for p in prompts if not brand_rx.search(p.get("text") or "")]}
    branded = []
    for seg, ids in groups.items():
        if ids == []:   # no prompt in this group (Peec rejects an empty filter)
            branded.append({**snap, "segment": seg, "n_prompts": 0, "visibility_brand": None})
            continue
        args = dict(base)
        if ids is not None:
            args["filters"] = [{"field": "prompt_id", "operator": "in", "values": ids}]
        row = next((r for r in pc.table("get_brand_report", args) if r.get("brand_id") == own), {})
        branded.append({**snap, "segment": seg, "n_prompts": len(prompts) if ids is None else len(ids),
                        "visibility_brand": row.get("visibility")})
    return daily_brand, daily_engine, sources, branded


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def fetch_start(existing_max, floor=HISTORY_START):
    """Backfill from `floor` when a tab is empty, else refetch the last REFETCH_DAYS."""
    if not existing_max:
        return floor
    if len(existing_max) == 7:   # monthly tab: "YYYY-MM"
        existing_max += "-01"
    return max(floor, (date.fromisoformat(existing_max[:10]) - timedelta(days=REFETCH_DAYS)).isoformat())
