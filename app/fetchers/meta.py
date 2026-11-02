import os
import time
from dotenv import load_dotenv
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

load_dotenv()


def _get_action_value(actions, *action_types):
    if not actions:
        return 0
    total = 0.0
    for action in actions:
        if action.get("action_type") in action_types:
            total += float(action.get("value", 0))
    return total


def _get_video_value(video_actions):
    if not video_actions:
        return ""
    return float(video_actions[0].get("value", 0))


def _build_row(insight, campaign_dates):
    campaign_id = insight.get("campaign_id", "")
    camp_start, camp_end = campaign_dates.get(campaign_id, ("", ""))

    actions = insight.get("actions", [])
    leads = _get_action_value(
        actions,
        "offsite_conversion.fb_pixel_lead",
        "onsite_conversion.lead_grouped",
    )
    three_sec_plays = _get_action_value(actions, "video_view")
    clicks = float(insight.get("clicks", 0) or 0)
    link_clicks = float(insight.get("inline_link_clicks", 0) or 0)
    reg_rate = round(leads / clicks * 100, 4) if clicks > 0 else ""

    has_video = bool(
        insight.get("video_p25_watched_actions")
        or insight.get("video_p100_watched_actions")
        or three_sec_plays
    )
    media_type = "Video" if has_video else "Image"

    return {
        "Channel": "Meta",
        "Date": insight.get("date_start", ""),
        "Campaign ID": campaign_id,
        "Campaign name": insight.get("campaign_name", ""),
        "Ad set ID": insight.get("adset_id", ""),
        "Ad ID": insight.get("ad_id", ""),
        "Ad set name": insight.get("adset_name", ""),
        "Ad name": insight.get("ad_name", ""),
        "Media type": media_type,
        "Start Date": camp_start,
        "End Date": camp_end,
        "Spend": float(insight.get("spend", 0) or 0),
        "Impressions": int(insight.get("impressions", 0) or 0),
        "Clicks": int(link_clicks),
        "CPM": round(float(insight.get("cpm", 0) or 0), 2),
        "CPC": round(float(insight.get("cpc", 0) or 0), 2),
        "CTR": round(float(insight.get("ctr", 0) or 0), 4),
        "Leads": leads,
        "3-second video plays": three_sec_plays if three_sec_plays else "",
        "Video average play time": _get_video_value(insight.get("video_avg_time_watched_actions")),
        "Video plays at 25%": _get_video_value(insight.get("video_p25_watched_actions")),
        "Video plays at 50%": _get_video_value(insight.get("video_p50_watched_actions")),
        "Video plays at 75%": _get_video_value(insight.get("video_p75_watched_actions")),
        "Video plays at 100%": _get_video_value(insight.get("video_p100_watched_actions")),
        "Registration Rate": reg_rate,
    }


def _run_async_insights(account, fields, start, end, campaign_dates,
                        poll_interval=10, max_wait_seconds=3600):
    params = {
        "level": "ad",
        "time_increment": 1,
        "time_range": {"since": start, "until": end},
    }

    print(f"  Submitting async insights job: {start} to {end}...")
    report = account.get_insights_async(fields=fields, params=params)
    report_id = report.get("id") or report.get("report_run_id")
    print(f"  Job submitted (id={report_id}). Polling Meta until it finishes...")

    elapsed = 0
    last_pct = -1
    while True:
        report.api_get()
        status = report.get("async_status", "")
        pct = int(report.get("async_percent_completion", 0) or 0)

        if status == "Job Completed":
            print(f"  Job completed (100%).")
            break
        if status in ("Job Failed", "Job Skipped"):
            raise RuntimeError(f"Meta insights job {status} (id={report_id})")
        if elapsed >= max_wait_seconds:
            raise TimeoutError(
                f"Meta insights job did not complete in {max_wait_seconds}s "
                f"(last status={status}, {pct}%, id={report_id})"
            )

        if pct != last_pct:
            print(f"    Status: {status} ({pct}%)")
            last_pct = pct

        time.sleep(poll_interval)
        elapsed += poll_interval

    print("  Fetching result pages...")
    rows = []
    for insight in report.get_result():
        rows.append(_build_row(insight, campaign_dates))
        if len(rows) % 500 == 0:
            print(f"    {len(rows)} rows fetched so far...")
    print(f"  {len(rows)} rows fetched.")
    return rows


def fetch_meta_data(start_date, end_date):
    access_token = os.getenv("META_ACCESS_TOKEN")
    ad_account_id = os.getenv("META_AD_ACCOUNT_ID")
    api_version = os.getenv("META_API_VERSION", "v21.0")

    FacebookAdsApi.init(access_token=access_token, api_version=api_version)
    account = AdAccount(ad_account_id)

    print("  Fetching campaign dates...")
    campaigns = account.get_campaigns(fields=["id", "name", "start_time", "stop_time"])
    campaign_dates = {}
    for c in campaigns:
        start = c.get("start_time", "")[:10] if c.get("start_time") else ""
        end = c.get("stop_time", "")[:10] if c.get("stop_time") else ""
        campaign_dates[c["id"]] = (start, end)
    print(f"  {len(campaign_dates)} campaigns loaded.")

    fields = [
        "date_start", "campaign_id", "campaign_name",
        "adset_id", "adset_name", "ad_id", "ad_name",
        "spend", "impressions", "clicks", "inline_link_clicks", "cpm", "cpc", "ctr",
        "actions",
        "video_avg_time_watched_actions",
        "video_p25_watched_actions", "video_p50_watched_actions",
        "video_p75_watched_actions", "video_p100_watched_actions",
    ]

    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            return _run_async_insights(account, fields, start_date, end_date, campaign_dates)
        except Exception as e:
            err_str = str(e)
            is_rate_limit = (
                "Application request limit reached" in err_str
                or '"code": 4' in err_str
                or '"code": 17' in err_str
                or "1504022" in err_str
            )
            if attempt < max_attempts:
                wait = 300 if is_rate_limit else 30 * attempt
                label = "rate limit" if is_rate_limit else "error"
                print(f"  Async job attempt {attempt} failed ({label}): retrying in {wait}s... ({e})")
                time.sleep(wait)
            else:
                print(f"  Async job failed after {max_attempts} attempts: {e}")
                return []
