"""
WhatsApp slider fetcher (GA4 -> aggregated daily rows).

Ported from the whatsapp-analytics repo (etl/build_chats_from_ga4.py,
etl/ga4_extract_sessions.py, etl/campaign_rules.py) so the same campaign rules
and step logic feed the Creative Performance dashboard's WhatsApp page.

PRIVACY: GA4 carries a pseudonymous user id (whatsapp_id = mc_<mc_id>_<session>)
and a free-text `message` (users sometimes type contact details into it). Both
are used ONLY in memory here, to dedup users and classify the entry message;
neither is ever returned, printed or written. Output = daily COUNTS per
campaign / landing, nothing per user.

History is always pulled in full from HISTORY_START: users are deduped across
days, so an incremental window would count a returning user twice. The volume
is tiny (a few thousand GA4 rows), so this costs seconds.

Returns three tables (see TABLES) for write_table() in sheets_writer.
"""

import os
import re
from collections import Counter, defaultdict
from datetime import date

from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

HISTORY_START = "2026-08-01"   # first whatsapp_slider event in GA4 is 2026-08-03

GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "")
# Service account with Viewer access on the GA4 property (GA4_SERVICE_ACCOUNT_JSON,
# falls back to the main Google service account file).
GA4_SA_DEFAULT = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")

CONTROL_LANDING = "landing/standard-form"
CONTROL_CAMPAIGN = "TEST_Control_Standard landing"

WHATSAPP_SLIDER = "whatsapp_slider"
CHAT_ACTIVATION = "chat_activation"
FOLLOWUP = "followup"
NOT_SET = "(not set)"
WHATSAPP_ID_RE = re.compile(r"^mc_(\d+)_(\d+)$")

# Slider steps by customEvent:event_value (stable across copy changes; the
# question TITLES change with each copy, so never key on those).
STEPS = [
    (1, "S1 CAP"),
    (2, "S2 Product"),
    (3, "S3 Timing"),
    (4, "S4 Building"),
    (5, "S5 Decision"),
    (6, "S6 Email"),
    (7, "S7 Phone"),
    (8, "S8 Marketing consent"),
    (9, "S9 Third-party consent"),
]

# ---------------------------------------------------------------------------
# Campaign rules — copied from whatsapp-analytics etl/campaign_rules.py.
# When the colleague adds a new entry message / campaign there, mirror it here.
# ---------------------------------------------------------------------------

# Internal test users (ManyChat subscriber ids), excluded from every count.
# Comma-separated in WHATSAPP_TEST_SUBSCRIBER_IDS.
TEST_MC_IDS = {s.strip() for s in os.getenv("WHATSAPP_TEST_SUBSCRIBER_IDS", "").split(",") if s.strip()}

# Ordered; first substring match on the chat_activation message wins.
ENTRY_MESSAGE_RULES = [
    ("continua_richiesta", "Continua con la richiesta."),
    ("proseguire_richiesta", "proseguire con la richiesta."),
    ("proseguo_richiesta", "Proseguo con la richiesta"),
    ("scoprire_bonus", "vorrei scoprire i bonus"),
    ("scopriamo_bonus", "scopriamo i miei bonus"),
]
ENTRY_MESSAGE_FALLBACK = "altro"

ENTRY_MESSAGE_CAMPAIGNS = {
    "continua_richiesta": "TEST_Whatsapp_Dual lead",
    "scoprire_bonus": "TEST_Whatsapp_Dual lead",
    "proseguo_richiesta": "TEST_Whatsapp_Single lead",
    "scopriamo_bonus": "TEST_Whatsapp_Single lead",
    "proseguire_richiesta": "TEST_Whatsapp_Pilot (Engagement/Lead)",
    "altro": "Altro / non identificata",
}
ENTRY_CAMPAIGN_FALLBACK = "Altro / non identificata"

# Same copy reused by a different campaign from a date on (inclusive).
ENTRY_MESSAGE_CAMPAIGN_DATE_OVERRIDES = {
    "scopriamo_bonus": ("2026-09-21", "TEST_Whatsapp_Dual lead (creative B)"),
}


def classify_entry_message(message):
    if not message:
        return ENTRY_MESSAGE_FALLBACK
    s = str(message)
    for label, substring in ENTRY_MESSAGE_RULES:
        if substring in s:
            return label
    return ENTRY_MESSAGE_FALLBACK


def campaign_for_entry_message(entry_type, entry_iso_date=None):
    override = ENTRY_MESSAGE_CAMPAIGN_DATE_OVERRIDES.get(entry_type)
    if override and entry_iso_date and entry_iso_date >= override[0]:
        return override[1]
    return ENTRY_MESSAGE_CAMPAIGNS.get(entry_type, ENTRY_CAMPAIGN_FALLBACK)


# ---------------------------------------------------------------------------
# GA4
# ---------------------------------------------------------------------------

def _session():
    path = os.getenv("GA4_SERVICE_ACCOUNT_JSON") or GA4_SA_DEFAULT
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"GA4 service account key not found: {path} (set GA4_SERVICE_ACCOUNT_JSON)")
    creds = service_account.Credentials.from_service_account_file(
        path, scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    return AuthorizedSession(creds)


def _run_report(session, dimensions, metric, dim_filter, start, end):
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}:runReport"
    body = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": metric}],
        "dimensionFilter": dim_filter,
        "limit": 100000,
    }
    out, offset = [], 0
    while True:
        body["offset"] = offset
        r = session.post(url, json=body)
        if r.status_code != 200:
            raise RuntimeError(f"GA4 API error {r.status_code}: {r.text[:300]}")
        d = r.json()
        page = d.get("rows", [])
        for row in page:
            vals = [v["value"] for v in row["dimensionValues"]]
            out.append((dict(zip(dimensions, vals)), float(row["metricValues"][0]["value"])))
        offset += len(page)
        if not page or offset >= int(d.get("rowCount", 0)):
            break
    return out


def _exact(field, value):
    return {"filter": {"fieldName": field, "stringFilter": {"value": value, "matchType": "EXACT"}}}


def _iso_from_dhm(dhm):
    """GA4 dateHourMinute 'YYYYMMDDHHMM' -> 'YYYY-MM-DD'."""
    return f"{dhm[:4]}-{dhm[4:6]}-{dhm[6:8]}"


def _mc_id(whatsapp_id):
    m = WHATSAPP_ID_RE.match(whatsapp_id or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

# "Chat activations" (chat_activation EVENTS) and "Sessions" are what the
# whatsapp-analytics Chat view counts ("Chat iniziate" / "Sessioni"); "Chat
# starts" is users with at least one activation.
CHAT_COLUMNS = (["Date", "Campaign", "UTM campaign", "Users", "Chat starts"]
                + [label for _, label in STEPS] + ["Followup users", "Chat activations", "Sessions"])
LANDING_COLUMNS = ["Date", "Landing", "Metric", "UTM campaign", "Sessions"]
INFO_COLUMNS = ["Key", "Value"]

LANDING_METRICS = {
    # (eventName, event_action or None) -> metric label
    ("session_start", None): "Landing sessions",
    ("Slider", "Compila form su whatsapp"): "Compila form su whatsapp",
    ("Slider", "Compila form online"): "Compila form online",
    ("Slider", "address_form"): "Online first question (address_form)",
    ("Slider", "slider-success"): "Online form completed (slider-success)",
}


def _build_chat_rows(session, start, end):
    dims = ["dateHourMinute", "customEvent:whatsapp_id", "customEvent:event_action",
            "customEvent:event_value", "customEvent:campaign"]
    events = _run_report(session, dims, "eventCount", _exact("eventName", WHATSAPP_SLIDER), start, end)

    # Entry message: queried separately and ONLY for chat_activation, so free
    # text typed later in the chat never enters this process. Classified on
    # the spot; the message itself is dropped.
    entry_filter = {"andGroup": {"expressions": [
        _exact("eventName", WHATSAPP_SLIDER),
        _exact("customEvent:event_action", CHAT_ACTIVATION),
    ]}}
    activations = _run_report(session, ["dateHourMinute", "customEvent:whatsapp_id", "customEvent:message"],
                              "eventCount", entry_filter, start, end)

    first_entry = {}          # mc_id -> (dhm, entry_type)
    for dimv, _ in activations:
        mc = _mc_id(dimv["customEvent:whatsapp_id"])
        if not mc or mc in TEST_MC_IDS:
            continue
        dhm = dimv["dateHourMinute"]
        if mc not in first_entry or dhm < first_entry[mc][0]:
            first_entry[mc] = (dhm, classify_entry_message(dimv["customEvent:message"]))
    del activations

    users = defaultdict(lambda: {"first": None, "steps": set(), "act": 0, "fu": False, "utm": Counter(),
                                 "sessions": set()})
    untracked = 0   # chat_activation events with no whatsapp_id (GA4 only has it from 2026-08-13)
    for dimv, n in events:
        mc = _mc_id(dimv["customEvent:whatsapp_id"])
        if not mc:
            if dimv["customEvent:event_action"] == CHAT_ACTIVATION:
                untracked += int(n)
            continue
        if mc in TEST_MC_IDS:
            continue
        u = users[mc]
        u["sessions"].add(WHATSAPP_ID_RE.match(dimv["customEvent:whatsapp_id"]).group(2))
        dhm = dimv["dateHourMinute"]
        if u["first"] is None or dhm < u["first"]:
            u["first"] = dhm
        action = dimv["customEvent:event_action"]
        if action == CHAT_ACTIVATION:
            u["act"] += int(n)
        elif action == FOLLOWUP:
            u["fu"] = True
        else:
            try:
                u["steps"].add(int(dimv["customEvent:event_value"]))
            except ValueError:
                pass
        utm = dimv["customEvent:campaign"]
        if utm and utm != NOT_SET:
            u["utm"][utm] += 1
    del events

    agg = defaultdict(lambda: Counter())
    for mc, u in users.items():
        entry = first_entry.get(mc)
        if entry:
            cohort_date = _iso_from_dhm(entry[0])
            campaign = campaign_for_entry_message(entry[1], cohort_date)
        else:
            cohort_date = _iso_from_dhm(u["first"])
            campaign = campaign_for_entry_message(ENTRY_MESSAGE_FALLBACK)
        utm = u["utm"].most_common(1)[0][0] if u["utm"] else NOT_SET
        c = agg[(cohort_date, campaign, utm)]
        c["Users"] += 1
        c["Chat starts"] += 1 if u["act"] else 0
        for n, label in STEPS:
            c[label] += 1 if n in u["steps"] else 0
        c["Followup users"] += 1 if u["fu"] else 0
        c["Chat activations"] += u["act"]
        c["Sessions"] += len(u["sessions"])

    rows = []
    for (d, campaign, utm), c in sorted(agg.items()):
        row = {"Date": d, "Campaign": campaign, "UTM campaign": utm}
        row.update({k: c.get(k, 0) for k in CHAT_COLUMNS[3:]})
        rows.append(row)
    return rows, len(users), untracked


def _landing_label(path):
    if "solo-whatsapp-demo" in path:
        return "solo-whatsapp-demo"
    return "whatsapp-demo"


def _build_landing_rows(session, start, end):
    dims = ["date", "landingPage", "eventName", "customEvent:event_action", "customEvent:campaign"]
    flt = {"filter": {"fieldName": "landingPage",
                      "stringFilter": {"value": "whatsapp-demo", "matchType": "CONTAINS"}}}
    agg = Counter()
    for dimv, sessions in _run_report(session, dims, "sessions", flt, start, end):
        ev, action = dimv["eventName"], dimv["customEvent:event_action"]
        metric = LANDING_METRICS.get((ev, None)) or LANDING_METRICS.get((ev, action))
        if not metric:
            continue
        d = dimv["date"]
        key = (f"{d[:4]}-{d[4:6]}-{d[6:8]}", _landing_label(dimv["landingPage"]), metric,
               dimv["customEvent:campaign"] or NOT_SET)
        agg[key] += sessions
    # Control landing (the report's baseline). It hosts several campaigns, so it
    # is isolated by sessionCampaignName — the same way the
    # manual report computes it.
    flt = {"andGroup": {"expressions": [
        {"filter": {"fieldName": "landingPage",
                    "stringFilter": {"value": CONTROL_LANDING, "matchType": "CONTAINS"}}},
        _exact("sessionCampaignName", CONTROL_CAMPAIGN),
    ]}}
    for dimv, sessions in _run_report(session, ["date", "eventName", "customEvent:event_action"],
                                      "sessions", flt, start, end):
        ev, action = dimv["eventName"], dimv["customEvent:event_action"]
        metric = ("Landing sessions" if ev == "session_start" else
                  "Control first question (address_form)" if (ev, action) == ("Slider", "address_form") else None)
        if metric:
            d = dimv["date"]
            agg[(f"{d[:4]}-{d[4:6]}-{d[6:8]}", "control", metric, CONTROL_CAMPAIGN)] += sessions
    return [{"Date": d, "Landing": lp, "Metric": m, "UTM campaign": utm, "Sessions": int(v)}
            for (d, lp, m, utm), v in sorted(agg.items())]


# ---------------------------------------------------------------------------
# The colleague's weekly Report snapshots (whatsapp-analytics data/reports.json):
# their hand-checked numbers (DB leads split online/WhatsApp, Excel-era chat
# counts before 07/09, control start, the improvised week-1 control) are the
# source of truth for the weeks they have frozen. Flattened to one row per
# week x campaign x field; the dashboard uses them instead of its live
# computation for those weeks. Aggregated campaign figures only.
# ---------------------------------------------------------------------------
REPORTS_JSON_DEFAULT = os.path.join(os.path.expanduser("~"), "Downloads", "whatsapp-analytics",
                                    "whatsapp-analytics", "data", "reports.json")
SNAPSHOT_COLUMNS = ["Week", "Label", "Start", "End", "Campaign", "Field", "Value"]
SNAPSHOT_FIELDS = ["campaign_name", "data_note", "spend", "impressions", "link_clicks", "active_days",
                   "start", "leads_sent",
                   "branches.online.branch_entry", "branches.online.step2_value", "branches.online.leads_sent",
                   "branches.whatsapp.branch_entry", "branches.whatsapp.step2_value", "branches.whatsapp.leads_sent"]


def load_report_snapshots(path=None):
    import json
    path = path or os.getenv("WHATSAPP_REPORTS_JSON") or REPORTS_JSON_DEFAULT
    if not os.path.exists(path):
        return None, path
    with open(path, encoding="utf-8") as f:
        weeks = json.load(f).get("weeks", [])
    rows = []
    for w in weeks:
        if w.get("is_aggregate"):
            continue   # the dashboard re-aggregates from the weekly rows
        for c in w.get("campaigns", []):
            for field in SNAPSHOT_FIELDS:
                v = c
                for k in field.split("."):
                    v = v.get(k) if isinstance(v, dict) else None
                rows.append({"Week": w["id"], "Label": w.get("label", ""),
                             "Start": w["period"]["start_date"], "End": w["period"]["end_date"],
                             # always text: gviz nulls the minority type in a mixed column
                             "Campaign": c["key"], "Field": field, "Value": "" if v is None else str(v)})
    return rows, path


TABLES = {
    "wa_chats_daily": CHAT_COLUMNS,
    "wa_landing_daily": LANDING_COLUMNS,
    "wa_info": INFO_COLUMNS,
}


def fetch_whatsapp_data():
    """Returns {tab_name: rows} for the WhatsApp sheet. Always full history."""
    start, end = HISTORY_START, date.today().isoformat()
    session = _session()
    chat_rows, n_users, untracked = _build_chat_rows(session, start, end)
    landing_rows = _build_landing_rows(session, start, end)
    info = [
        {"Key": "last_run", "Value": date.today().isoformat()},
        {"Key": "ga4_property", "Value": GA4_PROPERTY_ID},
        {"Key": "date_range", "Value": f"{start} to {end}"},
        {"Key": "users", "Value": n_users},
        {"Key": "untracked_chat_starts", "Value": untracked},
        {"Key": "untracked_note", "Value": "chat_activation events without a whatsapp_id (GA4 records it only "
                                           "from 2026-08-13): not in wa_chats_daily, campaign unknown."},
        {"Key": "note", "Value": "Users are counted once, on the date of their first chat_activation "
                                 "(cohort). Counts only; no user ids or message text are stored."},
    ]
    print(f"[ WhatsApp ] {n_users} users -> {len(chat_rows)} chat rows, {len(landing_rows)} landing rows "
          f"({start} to {end}).")
    return {"wa_chats_daily": chat_rows, "wa_landing_daily": landing_rows, "wa_info": info}
