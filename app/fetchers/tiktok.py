"""
TikTok Ads fetcher — SCAFFOLD, NOT YET TESTED against a real account.

Written ahead of API access being granted, from the documented TikTok
Marketing API v1.3 shape (business-api.tiktok.com). Once TIKTOK_ACCESS_TOKEN
and TIKTOK_ADVERTISER_ID are filled in (paste them in the "API Credentials"
panel in the app, or directly into .env), run:

    python main.py --source tiktok --target test

and diff the output against what the TikTok Ads Manager UI reports for the
same date range before ever pointing this at --target production. The parts
most likely to need adjusting once real responses are in hand are called out
in comments below (TODO-VERIFY).

API docs: https://business-api.tiktok.com/portal/docs (Reporting > Integrated Report).
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# TikTok's /report/integrated/get/ errors with code 40002 ("max time span is 30
# days") whenever stat_time_day is one of the dimensions and the range exceeds
# 30 days — confirmed 2026-08-21 against a real account on a 77-day range.
MAX_REPORT_SPAN_DAYS = 30


def _date_chunks(start_date, end_date, max_days=MAX_REPORT_SPAN_DAYS):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        chunks.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + timedelta(days=1)
    return chunks

TT_BASE_URL = "https://business-api.tiktok.com/open_api/v1.3"

# TODO-VERIFY: TikTok's reporting metrics don't include a generic "Leads" field —
# what counts as a lead depends on which conversion event is configured on the ad
# account (same situation as Outbrain's OUTBRAIN_LEAD_EVENT_NAME). "conversion" is
# the aggregate-conversions metric name in v1.3; if the account tracks a specific
# custom event instead, this may need to become "result" (with the ad's optimization
# goal) or a specific onsite/offsite event metric — check the first real response's
# `metrics` keys against https://business-api.tiktok.com/portal/docs?id=1707957200780290
# and adjust here.
LEAD_METRIC = os.getenv("TIKTOK_LEAD_METRIC", "conversion")

AD_FORMAT_TO_MEDIA = {
    "SINGLE_VIDEO": "Video",
    "SINGLE_IMAGE": "Image",
    "CAROUSEL_ADS": "Image",
    "COLLECTION_ADS": "Video",
}


def _headers():
    return {"Access-Token": os.getenv("TIKTOK_ACCESS_TOKEN"), "Content-Type": "application/json"}


def _get(path, params, max_attempts=5):
    """GET with TikTok's array/object params JSON-encoded, and basic rate-limit retry.
    TODO-VERIFY: confirm the actual rate-limit response shape (HTTP status + `code`
    field) once real calls are possible — this assumes HTTP 429 like Outbrain, but
    TikTok is documented to return HTTP 200 with a non-zero `code` for some errors."""
    encoded = dict(params)
    for k, v in encoded.items():
        if isinstance(v, (list, dict)):
            encoded[k] = json.dumps(v)
    for attempt in range(1, max_attempts + 1):
        resp = requests.get(f"{TT_BASE_URL}{path}", headers=_headers(), params=encoded)
        if resp.status_code == 429:
            wait = 10 * attempt
            print(f"    Rate limited — waiting {wait}s (attempt {attempt}/{max_attempts})...")
            time.sleep(wait)
            continue
        data = resp.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"TikTok API error {data.get('code')}: {data.get('message')} "
                                f"(path={path}, params={params})")
        return data
    raise RuntimeError(f"TikTok API rate-limited after {max_attempts} attempts (path={path})")


def _fetch_ad_metadata(advertiser_id):
    """Ad/ad-group/campaign names + ad format, keyed by ad_id. Paginated at page_size=1000
    (TikTok's documented max). TODO-VERIFY: confirm `create_time` is present at this field
    set for Start Date, or whether it needs to come from the campaign/adgroup objects
    instead (v1.3 sometimes nests schedule info under the adgroup, not the ad)."""
    meta = {}
    page = 1
    while True:
        data = _get("/ad/get/", {
            "advertiser_id": advertiser_id,
            "fields": ["ad_id", "ad_name", "adgroup_id", "adgroup_name",
                       "campaign_id", "campaign_name", "ad_format", "create_time"],
            "page": page,
            "page_size": 1000,
        })
        page_list = data.get("data", {}).get("list", [])
        for ad in page_list:
            meta[str(ad.get("ad_id"))] = ad
        page_info = data.get("data", {}).get("page_info", {})
        if page >= page_info.get("total_page", 1):
            break
        page += 1
    return meta


def _build_row(entry, ad_meta):
    dims = entry.get("dimensions", {})
    metrics = entry.get("metrics", {})
    ad_id = str(dims.get("ad_id", ""))
    meta = ad_meta.get(ad_id, {})

    # TODO-VERIFY: stat_time_day comes back as "YYYY-MM-DD HH:MM:SS" in v1.3 — trim to date.
    date_val = (dims.get("stat_time_day") or "")[:10]

    def num(key, default=0):
        v = metrics.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    spend = num("spend")
    impressions = num("impressions")
    clicks = num("clicks")
    cpm = num("cpm")
    cpc = num("cpc")
    # VERIFIED 2026-08-21 against a real account: TikTok's `ctr` metric is already a
    # percentage (e.g. "0.54" means 0.54%), unlike Outbrain's ctr field. Confirmed by
    # cross-checking clicks/impressions*100 against the raw value for the same rows —
    # scaling values <=1 by 100x (the old heuristic) inflated most CTRs by 100x.
    ctr = round(num("ctr"), 4)
    leads = num(LEAD_METRIC)
    reg_rate = round(leads / clicks * 100, 4) if clicks > 0 else ""

    ad_format = meta.get("ad_format", "")
    media_type = AD_FORMAT_TO_MEDIA.get(ad_format, "")

    return {
        "Channel": "TikTok",
        "Date": date_val,
        "Campaign ID": meta.get("campaign_id", ""),
        "Campaign name": meta.get("campaign_name", ""),
        "Ad set ID": meta.get("adgroup_id", ""),
        "Ad ID": ad_id,
        "Ad set name": meta.get("adgroup_name", ""),
        "Ad name": meta.get("ad_name", ""),
        "Media type": media_type,
        "Start Date": (meta.get("create_time") or "")[:10],
        "End Date": "",
        "Spend": round(spend, 2),
        "Impressions": int(impressions),
        "Clicks": int(clicks),
        "CPM": round(cpm, 2),
        "CPC": round(cpc, 2),
        "CTR": ctr,
        "Leads": leads,
        "3-second video plays": num("video_watched_2s", "") or "",
        "Video average play time": num("average_video_play", "") or "",
        "Video plays at 25%": num("video_views_p25", "") or "",
        "Video plays at 50%": num("video_views_p50", "") or "",
        "Video plays at 75%": num("video_views_p75", "") or "",
        "Video plays at 100%": num("video_views_p100", "") or "",
        "Registration Rate": reg_rate,
    }


def fetch_tiktok_data(start_date, end_date):
    advertiser_id = os.getenv("TIKTOK_ADVERTISER_ID")
    if not os.getenv("TIKTOK_ACCESS_TOKEN") or not advertiser_id:
        print("  TIKTOK_ACCESS_TOKEN / TIKTOK_ADVERTISER_ID not set — paste them into the "
              "API Credentials panel (or .env) once access is approved. Skipping.")
        return []

    print("  Fetching ad/campaign metadata...")
    ad_meta = _fetch_ad_metadata(advertiser_id)
    print(f"  {len(ad_meta)} ads found.")

    # TODO-VERIFY: `metrics` list below covers the commonly-documented BASIC report
    # fields for AUCTION_AD data_level — confirm every name against the real response
    # (a field TikTok doesn't recognize for this account/report type will error the
    # whole call, not just omit that column).
    metrics = [
        "spend", "impressions", "clicks", "ctr", "cpm", "cpc", LEAD_METRIC,
        "video_watched_2s", "average_video_play",
        "video_views_p25", "video_views_p50", "video_views_p75", "video_views_p100",
    ]

    chunks = _date_chunks(start_date, end_date)
    print(f"  Fetching integrated report: {start_date} to {end_date} "
          f"({len(chunks)} chunk(s) of <= {MAX_REPORT_SPAN_DAYS} days)...")
    rows = []
    for chunk_start, chunk_end in chunks:
        print(f"    Chunk {chunk_start} to {chunk_end}...")
        page = 1
        while True:
            data = _get("/report/integrated/get/", {
                "advertiser_id": advertiser_id,
                "report_type": "BASIC",
                "data_level": "AUCTION_AD",
                "dimensions": ["ad_id", "stat_time_day"],
                "metrics": metrics,
                "start_date": chunk_start,
                "end_date": chunk_end,
                "page": page,
                "page_size": 1000,
            })
            page_list = data.get("data", {}).get("list", [])
            for entry in page_list:
                rows.append(_build_row(entry, ad_meta))
            page_info = data.get("data", {}).get("page_info", {})
            print(f"      Page {page}/{page_info.get('total_page', 1)} — {len(page_list)} rows.")
            if page >= page_info.get("total_page", 1):
                break
            page += 1

    print(f"  {len(rows)} rows fetched.")
    return rows
