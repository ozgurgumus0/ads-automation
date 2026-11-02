import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

OB_BASE_URL = "https://api.outbrain.com/amplify/v0.1"

# Outbrain conversion event whose click-attributed conversions populate the "Leads" column.
# The Outbrain report exposes a per-event breakdown in `metrics.conversionMetrics[]`
# (when called with includeConversionDetails=true) — match the entry whose `name` contains this string.
LEAD_EVENT_NAME = os.getenv("OUTBRAIN_LEAD_EVENT_NAME", "bestaetigung italy")


def _headers():
    return {"OB-TOKEN-V1": os.getenv("OUTBRAIN_API_KEY")}


def _extract_event_leads(m):
    """Return the click-attributed conversions for LEAD_EVENT_NAME from the row's metrics.
    Falls back to the total `conversions` field if the event isn't present."""
    target = LEAD_EVENT_NAME.lower().strip()
    for entry in (m.get("conversionMetrics") or []):
        if target in (entry.get("name") or "").lower():
            return float(entry.get("conversions", 0) or 0)
    # Fallback: total conversions across all events
    return float(m.get("conversions", m.get("totalConversions", 0)) or 0)


def _parse_row(m, date, campaign_id, campaign_name, media_type, camp_start, camp_end, ad_id="", ad_name="", url=""):
    spend = float(m.get("spend", 0) or 0)
    impressions = int(m.get("impressions", 0) or 0)
    clicks = int(m.get("clicks", 0) or 0)
    cpc = float(m.get("cpc", m.get("ecpc", 0)) or 0)
    ctr_raw = float(m.get("ctr", 0) or 0)
    ctr = round(ctr_raw * 100, 4) if ctr_raw <= 1 else round(ctr_raw, 4)
    cpm = round(spend / impressions * 1000, 2) if impressions > 0 else 0

    return {
        "Channel": "Outbrain",
        "Date": date,
        "Campaign ID": campaign_id,
        "Campaign name": campaign_name,
        "Ad set ID": "",
        "Ad ID": ad_id,
        "Ad set name": "",
        "Ad name": ad_name,
        "Media type": media_type,
        "Start Date": camp_start,
        "End Date": camp_end,
        "Spend": round(spend, 2),
        "Impressions": impressions,
        "Clicks": clicks,
        "CPM": cpm,
        "CPC": round(cpc, 2),
        "CTR": ctr,
        "Leads": _extract_event_leads(m),
        "3-second video plays": "",
        "Video average play time": "",
        "Video plays at 25%": "",
        "Video plays at 50%": "",
        "Video plays at 75%": "",
        "Video plays at 100%": "",
        "Registration Rate": "",
        "URL": url,
    }


def fetch_outbrain_data(start_date, end_date):
    account_id = os.getenv("OUTBRAIN_ACCOUNT_ID")
    headers = _headers()

    # Fetch all campaigns (paginated) for metadata
    print("  Fetching campaigns...")
    campaigns = []
    offset = 0
    while True:
        resp = requests.get(
            f"{OB_BASE_URL}/marketers/{account_id}/campaigns",
            headers=headers,
            params={"limit": 25, "offset": offset},
        )
        resp.raise_for_status()
        page = resp.json().get("campaigns", [])
        if not page:
            break
        campaigns.extend(page)
        offset += len(page)
    campaign_meta = {c["id"]: c for c in campaigns}
    print(f"  {len(campaigns)} campaigns found.")

    # Fetch campaign-level report to identify which campaigns had activity
    print("  Fetching campaign activity report...")
    campaign_results = []
    offset = 0
    while True:
        report_resp = requests.get(
            f"{OB_BASE_URL}/reports/marketers/{account_id}/campaigns/periodic",
            headers=headers,
            params={"from": start_date, "to": end_date, "breakdown": "daily", "limit": 100, "offset": offset},
        )
        report_resp.raise_for_status()
        page = report_resp.json().get("campaignResults", [])
        if not page:
            break
        campaign_results.extend(page)
        if len(page) < 100:
            break
        offset += len(page)

    # Resolve any campaign IDs not in our cache
    active_campaign_ids = {r.get("campaignId", "") for r in campaign_results if r.get("campaignId")}
    for cid in active_campaign_ids:
        if cid not in campaign_meta:
            resp = requests.get(f"{OB_BASE_URL}/campaigns/{cid}", headers=headers)
            if resp.status_code == 200:
                campaign_meta[cid] = resp.json()

    print(f"  {len(active_campaign_ids)} active campaigns in date range.")

    # For each active campaign fetch per-ad periodic report via periodicContent
    print("  Fetching per-ad performance per campaign...")
    rows = []
    active_campaign_list = list(active_campaign_ids)
    total = len(active_campaign_list)
    for idx, cid in enumerate(active_campaign_list, 1):
        pct = round(idx / total * 100)
        camp_info = campaign_meta.get(cid, {})
        campaign_id = camp_info.get("internalId", "")
        campaign_name = camp_info.get("name", "")
        media_type = camp_info.get("creativeFormat", "")
        camp_start = camp_info.get("budget", {}).get("startDate", "")
        camp_end = camp_info.get("budget", {}).get("endDate", "")

        print(f"  [{idx}/{total} — {pct}%] {campaign_name or cid}")

        # Fetch promoted link names and URLs for this campaign (paginated)
        pl_name_map = {}
        pl_url_map = {}
        pl_offset = 0
        while True:
            pl_resp = requests.get(
                f"{OB_BASE_URL}/campaigns/{cid}/promotedLinks",
                headers=headers,
                params={"limit": 100, "offset": pl_offset},
            )
            if pl_resp.status_code != 200:
                break
            pl_page = pl_resp.json().get("promotedLinks", [])
            if not pl_page:
                break
            for pl in pl_page:
                pid = pl["id"]
                pl_name_map[pid] = pl.get("metaData", "")
                # URL may be top-level or nested under "content"
                url_val = (
                    pl.get("url")
                    or pl.get("content", {}).get("url", "")
                    or pl.get("targetUrl", "")
                    or ""
                )
                pl_url_map[pid] = url_val
            if len(pl_page) < 100:
                break
            pl_offset += len(pl_page)

        # Fetch per-ad periodic report. includeConversionDetails=true adds the
        # `metrics.conversionMetrics` per-event breakdown we use to populate Leads.
        ad_params = {
            "from": start_date, "to": end_date,
            "breakdown": "daily", "limit": 500,
            "includeConversionDetails": "true",
        }
        ad_report_resp = None
        for attempt in range(1, 6):
            resp = requests.get(
                f"{OB_BASE_URL}/reports/marketers/{account_id}/campaigns/{cid}/periodicContent",
                headers=headers,
                params=ad_params,
            )
            if resp.status_code == 429:
                wait = 10 * attempt
                print(f"    [{cid}] Rate limited — waiting {wait}s (attempt {attempt}/5)...")
                time.sleep(wait)
                continue
            ad_report_resp = resp
            break
        if ad_report_resp is None or ad_report_resp.status_code != 200:
            code = ad_report_resp.status_code if ad_report_resp else "no response"
            text = ad_report_resp.text[:200] if ad_report_resp else ""
            print(f"    [{cid}] HTTP {code}: {text}")
            continue
        time.sleep(0.5)  # small pause between campaigns to stay under rate limit

        pl_results = ad_report_resp.json().get("promotedLinkResults", [])
        for pl_result in pl_results:
            ad_id = pl_result.get("promotedLinkId", "")
            ad_name = pl_name_map.get(ad_id, "")
            url = pl_url_map.get(ad_id, "")
            for day in pl_result.get("results", []):
                metadata = day.get("metadata", day)
                metrics = day.get("metrics", day)
                date = metadata.get("fromDate", "")
                rows.append(_parse_row(metrics, date, campaign_id, campaign_name, media_type, camp_start, camp_end, ad_id, ad_name, url))

    print(f"  {len(rows)} ad-day rows fetched.")
    return rows
