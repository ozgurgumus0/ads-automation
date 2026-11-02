import os
import time
from datetime import date, timedelta
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient

load_dotenv()

AD_TYPE_TO_MEDIA = {
    "VIDEO_RESPONSIVE_AD": "Video",
    "VIDEO_BUMPER_AD": "Video",
    "VIDEO_NON_SKIPPABLE_IN_STREAM_AD": "Video",
    "VIDEO_OUTSTREAM_AD": "Video",
    "VIDEO_TRUE_VIEW_IN_DISPLAY_AD": "Video",
    "VIDEO_TRUE_VIEW_IN_STREAM_AD": "Video",
    "IMAGE_AD": "Image",
    "RESPONSIVE_DISPLAY_AD": "Display",
    "RESPONSIVE_SEARCH_AD": "Search",
    "EXPANDED_TEXT_AD": "Search",
    "SMART_CAMPAIGN_AD": "Smart",
}


def _build_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
        "use_proto_plus": True,
    })


def _month_chunks(start_date, end_date):
    from datetime import datetime
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    chunks = []
    cursor = start
    while cursor <= end:
        month_end = date(cursor.year + (cursor.month // 12), cursor.month % 12 + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, end)
        chunks.append((cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _fetch_chunk(ga_service, customer_id, start, end):
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            campaign.advertising_channel_type,
            ad_group.id,
            ad_group.name,
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.ad.type,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.expanded_text_ad.headline_part1,
            ad_group_ad.ad.expanded_text_ad.headline_part2,
            ad_group_ad.ad.responsive_display_ad.headlines,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.average_cpm,
            metrics.average_cpc,
            metrics.ctr,
            metrics.conversions,
            metrics.video_quartile_p25_rate,
            metrics.video_quartile_p50_rate,
            metrics.video_quartile_p75_rate,
            metrics.video_quartile_p100_rate
        FROM ad_group_ad
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY segments.date
    """
    response = ga_service.search_stream(customer_id=customer_id, query=query)

    rows = []
    for batch in response:
        for row in batch.results:
            camp_name = row.campaign.name
            channel_type = row.campaign.advertising_channel_type.name \
                if hasattr(row.campaign.advertising_channel_type, "name") else ""
            if "YT" in camp_name:
                channel = "Youtube"
            elif "DS" in camp_name:
                channel = "Display"
            elif channel_type == "SEARCH":
                channel = "Google Search"
            else:
                continue

            impressions = row.metrics.impressions
            spend = row.metrics.cost_micros / 1_000_000
            clicks = row.metrics.clicks
            cpm = row.metrics.average_cpm / 1_000_000
            cpc = row.metrics.average_cpc / 1_000_000
            ctr = round(row.metrics.ctr * 100, 4)
            leads = row.metrics.conversions

            def video_plays(rate):
                if impressions > 0 and rate > 0:
                    return round(rate * impressions)
                return ""

            ad_type_name = row.ad_group_ad.ad.type_.name if hasattr(row.ad_group_ad.ad.type_, "name") else ""
            media_type = AD_TYPE_TO_MEDIA.get(ad_type_name, ad_type_name.replace("_AD", "").title())
            reg_rate = round(leads / clicks * 100, 4) if clicks > 0 else ""

            # Responsive Search Ads (and many Display ads) leave ad.name blank.
            # Fall back to RSA headlines, then expanded-text-ad headlines, then RDA headlines
            # so every row carries a name the dashboard can join against.
            ad_name = row.ad_group_ad.ad.name or ""
            if not ad_name:
                rsa_headlines = [h.text for h in row.ad_group_ad.ad.responsive_search_ad.headlines if h.text]
                if rsa_headlines:
                    ad_name = " | ".join(rsa_headlines[:3])
                elif row.ad_group_ad.ad.expanded_text_ad.headline_part1:
                    parts = [
                        row.ad_group_ad.ad.expanded_text_ad.headline_part1,
                        row.ad_group_ad.ad.expanded_text_ad.headline_part2,
                    ]
                    ad_name = " | ".join(p for p in parts if p)
                else:
                    rda_headlines = [h.text for h in row.ad_group_ad.ad.responsive_display_ad.headlines if h.text]
                    if rda_headlines:
                        ad_name = " | ".join(rda_headlines[:3])

            rows.append({
                "Channel": channel,
                "Date": row.segments.date,
                "Campaign ID": str(row.campaign.id),
                "Campaign name": camp_name,
                "Ad set ID": str(row.ad_group.id),
                "Ad ID": str(row.ad_group_ad.ad.id),
                "Ad set name": row.ad_group.name,
                "Ad name": ad_name,
                "Media type": media_type,
                "Start Date": "",
                "End Date": "",
                "Spend": round(spend, 2),
                "Impressions": impressions,
                "Clicks": clicks,
                "CPM": round(cpm, 2),
                "CPC": round(cpc, 2),
                "CTR": ctr,
                "Leads": leads,
                "3-second video plays": "",
                "Video average play time": "",
                "Video plays at 25%": video_plays(row.metrics.video_quartile_p25_rate),
                "Video plays at 50%": video_plays(row.metrics.video_quartile_p50_rate),
                "Video plays at 75%": video_plays(row.metrics.video_quartile_p75_rate),
                "Video plays at 100%": video_plays(row.metrics.video_quartile_p100_rate),
                "Registration Rate": reg_rate,
            })
    return rows


def _fetch_pmax_chunk(ga_service, customer_id, start, end):
    # PMax has no ad_group_ad; query at asset_group (the PMax analog of ad groups).
    # average_cpm / average_cpc are not exposed on asset_group, so derive them.
    query = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            asset_group.id,
            asset_group.name,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr,
            metrics.conversions
        FROM asset_group
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.advertising_channel_type = 'PERFORMANCE_MAX'
        ORDER BY segments.date
    """
    response = ga_service.search_stream(customer_id=customer_id, query=query)

    rows = []
    for batch in response:
        for row in batch.results:
            spend = row.metrics.cost_micros / 1_000_000
            impressions = row.metrics.impressions
            clicks = row.metrics.clicks
            leads = row.metrics.conversions
            cpm = round(spend / impressions * 1000, 2) if impressions > 0 else 0
            cpc = round(spend / clicks, 2) if clicks > 0 else 0
            ctr = round(row.metrics.ctr * 100, 4)
            reg_rate = round(leads / clicks * 100, 4) if clicks > 0 else ""

            asset_group_id = str(row.asset_group.id)
            asset_group_name = row.asset_group.name or ""

            rows.append({
                "Channel": "PMax",
                "Date": row.segments.date,
                "Campaign ID": str(row.campaign.id),
                "Campaign name": row.campaign.name,
                "Ad set ID": asset_group_id,
                "Ad ID": asset_group_id,
                "Ad set name": asset_group_name,
                "Ad name": asset_group_name,
                "Media type": "PMax",
                "Start Date": "",
                "End Date": "",
                "Spend": round(spend, 2),
                "Impressions": impressions,
                "Clicks": clicks,
                "CPM": cpm,
                "CPC": cpc,
                "CTR": ctr,
                "Leads": leads,
                "3-second video plays": "",
                "Video average play time": "",
                "Video plays at 25%": "",
                "Video plays at 50%": "",
                "Video plays at 75%": "",
                "Video plays at 100%": "",
                "Registration Rate": reg_rate,
            })
    return rows


def fetch_google_ads_data(start_date, end_date):
    client = _build_client()
    customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID").replace("-", "")
    ga_service = client.get_service("GoogleAdsService")

    chunks = _month_chunks(start_date, end_date)
    all_rows = []
    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        print(f"  Fetching chunk {i}/{len(chunks)} (Search/Youtube/Display): {chunk_start} to {chunk_end}...")
        for attempt in range(1, 4):
            try:
                chunk_rows = _fetch_chunk(ga_service, customer_id, chunk_start, chunk_end)
                print(f"    {len(chunk_rows)} rows.")
                all_rows.extend(chunk_rows)
                break
            except Exception as e:
                if attempt < 3:
                    wait = attempt * 10
                    print(f"    Attempt {attempt} failed: {e}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"    Chunk {chunk_start} to {chunk_end} failed after 3 attempts: {e}")

        print(f"  Fetching chunk {i}/{len(chunks)} (PMax): {chunk_start} to {chunk_end}...")
        for attempt in range(1, 4):
            try:
                pmax_rows = _fetch_pmax_chunk(ga_service, customer_id, chunk_start, chunk_end)
                print(f"    {len(pmax_rows)} rows.")
                all_rows.extend(pmax_rows)
                break
            except Exception as e:
                if attempt < 3:
                    wait = attempt * 10
                    print(f"    Attempt {attempt} failed: {e}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"    PMax chunk {chunk_start} to {chunk_end} failed after 3 attempts: {e}")

    return all_rows
