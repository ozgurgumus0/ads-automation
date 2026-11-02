"""
Fetches transcripts / ad copy for every ad in the Transkripts sheet.

Strategy per channel + media type:
  Meta  / Image   → Meta Creative API (body + title) + GPT-4o vision on image URL
  Meta  / Video   → Meta Creative API → video download URL → OpenAI Whisper
  Youtube         → Google Ads API → YouTube video ID → youtube-transcript-api
  Display         → Google Ads API → image asset URL → GPT-4o vision

Resumable: rows that already have a non-empty Transcript are skipped.

Usage:
  python transcribe_ads.py

Requires OPENAI_API_KEY in .env
"""

import os
import re
import sys
import time
import base64
import tempfile

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv
import gspread
from openai import OpenAI
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

load_dotenv(override=True)  # override any system env vars with .env values

SPREADSHEET_ID  = "__SHEET_ID__"
SA_FILE         = "service_account.json"
META_TOKEN      = os.getenv("META_ACCESS_TOKEN")
META_VERSION    = os.getenv("META_API_VERSION", "v21.0")
CUSTOMER_ID     = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")


# ── helpers ───────────────────────────────────────────────────────────────────

def meta_get(path, params=None):
    params = params or {}
    params["access_token"] = META_TOKEN
    url = f"https://graph.facebook.com/{META_VERSION}/{path}"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def image_to_b64(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return base64.b64encode(r.content).decode()


def gpt4o_vision(client: OpenAI, image_url: str, prompt: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
        max_tokens=500,
    )
    return resp.choices[0].message.content.strip()


# ── Meta ──────────────────────────────────────────────────────────────────────

def get_meta_creative_fields(ad_id: str) -> dict:
    """Returns creative fields: body, title, image_url, video_id."""
    ad_data = meta_get(str(ad_id), {"fields": "creative"})
    creative_id = ad_data.get("creative", {}).get("id")
    if not creative_id:
        return {}
    creative = meta_get(str(creative_id), {
        "fields": "body,title,image_url,video_id,object_story_spec"
    })
    # Fallback: dig into object_story_spec for body/title if top-level is missing
    spec = creative.get("object_story_spec", {})
    link_data = spec.get("link_data") or spec.get("video_data") or {}
    return {
        "body":      creative.get("body") or link_data.get("message", ""),
        "title":     creative.get("title") or link_data.get("name", ""),
        "image_url": creative.get("image_url") or link_data.get("image_url", ""),
        "video_id":  creative.get("video_id") or link_data.get("video_id", ""),
    }


def transcribe_meta_image(ad_id: str, client: OpenAI) -> str:
    fields = get_meta_creative_fields(ad_id)
    parts = []
    if fields.get("title"):
        parts.append(f"Headline: {fields['title']}")
    if fields.get("body"):
        parts.append(f"Body: {fields['body']}")
    copy_text = "\n".join(parts)

    if fields.get("image_url"):
        try:
            vision_text = gpt4o_vision(
                client,
                fields["image_url"],
                "Extract ALL visible text from this ad image exactly as it appears. "
                "List each text element on its own line. Do not describe the image.",
            )
            if vision_text:
                copy_text = (copy_text + "\n\nOn-screen text:\n" + vision_text).strip()
        except Exception as e:
            print(f"    GPT-4o vision failed: {e}")

    return copy_text or "[No text found]"


def transcribe_meta_video(ad_id: str, client: OpenAI) -> str:
    fields = get_meta_creative_fields(ad_id)
    video_id = fields.get("video_id")
    ad_copy = "\n".join(filter(None, [
        f"Headline: {fields['title']}" if fields.get("title") else "",
        f"Body: {fields['body']}"     if fields.get("body")  else "",
    ]))

    if not video_id:
        return (ad_copy + "\n[No video_id found in creative]").strip() or "[No video_id found in creative]"

    # Try fetching video source — requires ads_read + video access
    source_url = None
    try:
        video_data = meta_get(str(video_id), {"fields": "source,description,embeddable_url"})
        source_url = video_data.get("source") or video_data.get("embeddable_url")
    except Exception as e:
        print(f"    Video source fetch failed: {e}")

    if not source_url:
        # Fallback: try captions endpoint
        try:
            captions_data = meta_get(f"{video_id}/captions", {})
            captions = captions_data.get("data", [])
            if captions:
                caption_url = captions[0].get("uri", "")
                if caption_url:
                    r = requests.get(caption_url, timeout=30)
                    caption_text = r.text.strip()
                    # Strip SRT timestamps, keep text lines
                    lines = [l.strip() for l in caption_text.splitlines()
                             if l.strip() and not l.strip().isdigit()
                             and "-->" not in l]
                    return (ad_copy + "\n\nVideo captions:\n" + " ".join(lines)).strip()
        except Exception as e:
            print(f"    Caption fallback failed: {e}")
        return (ad_copy + "\n[Video source not accessible via API]").strip() or "[Video source not accessible via API]"

    # Download to temp file and transcribe with Whisper
    try:
        r = requests.get(source_url, timeout=120, stream=True)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                tmp.write(chunk)
            tmp_path = tmp.name
        try:
            with open(tmp_path, "rb") as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text",
                )
            text = transcript.strip() if isinstance(transcript, str) else str(transcript)
            return (ad_copy + "\n\nSpoken transcript:\n" + text).strip()
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        print(f"    Whisper transcription failed: {e}")
        return (ad_copy + f"\n[Whisper failed: {e}]").strip()


# ── Google Ads ────────────────────────────────────────────────────────────────

def build_ga_client():
    from google.ads.googleads.client import GoogleAdsClient
    return GoogleAdsClient.load_from_dict({
        "developer_token":    os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":          os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":      os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":      os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id":  os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
        "use_proto_plus":     True,
    })


def get_youtube_video_id(ga_service, customer_id: str, ad_id: str) -> str | None:
    """Query Google Ads for a YouTube video ID from a video-responsive ad."""
    int_id = int(float(ad_id))
    query = f"""
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.video_responsive_ad.videos,
            ad_group_ad.ad.demand_gen_video_responsive_ad.videos
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id = {int_id}
        LIMIT 1
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            ad = row.ad_group_ad.ad
            video_lists = (
                list(ad.video_responsive_ad.videos)
                + list(ad.demand_gen_video_responsive_ad.videos)
            )
            for asset_resource in video_lists:
                # resource name: customers/.../assets/...
                asset_id = asset_resource.split("/")[-1]
                # Fetch the asset to get YouTube video ID
                asset_query = f"""
                    SELECT asset.youtube_video_asset.youtube_video_id
                    FROM asset
                    WHERE asset.id = {asset_id}
                    LIMIT 1
                """
                asset_resp = ga_service.search(customer_id=customer_id, query=asset_query)
                for asset_row in asset_resp:
                    yt_id = asset_row.asset.youtube_video_asset.youtube_video_id
                    if yt_id:
                        return yt_id
    except Exception as e:
        print(f"    Google Ads query failed for ad {ad_id}: {e}")
    return None


def transcribe_youtube(ad_id: str, ga_service, customer_id: str) -> str:
    yt_video_id = get_youtube_video_id(ga_service, customer_id, ad_id)
    if not yt_video_id:
        return "[Could not retrieve YouTube video ID]"

    for lang in ["it", "en", "de"]:
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(yt_video_id, languages=[lang])
            text = " ".join(t["text"] for t in transcript_list)
            return f"[YouTube auto-transcript ({lang})]\n{text}"
        except (NoTranscriptFound, TranscriptsDisabled):
            continue
        except Exception as e:
            print(f"    YT transcript error ({lang}): {e}")
            continue

    return f"[No transcript available for YouTube video {yt_video_id}]"


def get_display_image_url(ga_service, customer_id: str, ad_id: str) -> str | None:
    """Fetch a marketing image URL from a Demand Gen multi-asset ad."""
    int_id = int(float(ad_id))
    query = f"""
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.demand_gen_multi_asset_ad.marketing_images,
            ad_group_ad.ad.demand_gen_multi_asset_ad.square_marketing_images
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id = {int_id}
        LIMIT 1
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            ad = row.ad_group_ad.ad
            assets = (
                list(ad.demand_gen_multi_asset_ad.marketing_images)
                + list(ad.demand_gen_multi_asset_ad.square_marketing_images)
            )
            for asset_resource in assets:
                asset_id = asset_resource.split("/")[-1]
                asset_query = f"""
                    SELECT asset.image_asset.full_size.url
                    FROM asset
                    WHERE asset.id = {asset_id}
                    LIMIT 1
                """
                asset_resp = ga_service.search(customer_id=customer_id, query=asset_query)
                for asset_row in asset_resp:
                    url = asset_row.asset.image_asset.full_size.url
                    if url:
                        return url
    except Exception as e:
        print(f"    Google Ads display image query failed for ad {ad_id}: {e}")
    return None


def transcribe_display(ad_id: str, ga_service, customer_id: str, client: OpenAI) -> str:
    image_url = get_display_image_url(ga_service, customer_id, ad_id)
    if not image_url:
        return "[Could not retrieve display image URL]"
    try:
        return gpt4o_vision(
            client,
            image_url,
            "Extract ALL visible text from this display ad image exactly as it appears. "
            "List each text element on its own line. Do not describe the image.",
        )
    except Exception as e:
        return f"[GPT-4o vision failed: {e}]"


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        print("ERROR: OPENAI_API_KEY not set in .env — required for image/video processing.")
        sys.exit(1)

    oai = OpenAI(api_key=openai_key)

    print("Connecting to Google Sheets…")
    gc = gspread.service_account(filename=SA_FILE)
    sp = gc.open_by_key(SPREADSHEET_ID)

    print("Reading Transkripts tab…")
    transkripts = sp.worksheet("Transkripts")
    all_values  = transkripts.get_all_values()
    header      = all_values[0]  # ['Ad Name', 'Channel', 'Media Type', 'Transcript']
    rows        = all_values[1:]

    col_name        = header.index("Ad Name")
    col_channel     = header.index("Channel")
    col_media       = header.index("Media Type")
    col_transcript  = header.index("Transcript")

    print("Reading main_metrics for Ad ID lookup…")
    src     = sp.worksheet("main_metrics")
    records = src.get_all_records()

    # Build ad_name (lower) → first matching ad_id
    ad_id_map = {}
    for r in records:
        name = str(r.get("Ad name", "")).strip()
        key  = name.lower()
        if name and key not in ad_id_map:
            ad_id_map[key] = {
                "ad_id":      str(r.get("Ad ID", "")).strip(),
                "channel":    str(r.get("Channel", "")).strip(),
                "media_type": str(r.get("Media type", "")).strip(),
            }

    print("Building Google Ads client…")
    try:
        ga_client  = build_ga_client()
        ga_service = ga_client.get_service("GoogleAdsService")
    except Exception as e:
        print(f"  WARNING: Google Ads client failed: {e}")
        ga_service = None

    col_letter = chr(ord("A") + col_transcript)

    def _flush(sheet, writes: dict, col_idx: int):
        if not writes:
            return
        cl = chr(ord("A") + col_idx)
        data = [{"range": f"Transkripts!{cl}{r}", "values": [[v]]} for r, v in writes.items()]
        sheet.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": data})
        print(f"  [batch] Wrote {len(data)} cells to sheet.")
        writes.clear()

    pending_writes = {}
    total  = len(rows)
    done   = 0
    errors = 0

    for i, row in enumerate(rows):
        # Pad short rows
        while len(row) < len(header):
            row.append("")

        ad_name    = row[col_name].strip()
        channel    = row[col_channel].strip()
        media_type = row[col_media].strip()
        existing   = row[col_transcript].strip()

        sheet_row = i + 2  # 1-indexed, +1 for header

        if existing:
            print(f"[{i+1}/{total}] SKIP (already done): {ad_name}")
            continue

        meta_info = ad_id_map.get(ad_name.lower(), {})
        ad_id     = meta_info.get("ad_id", "")

        if not ad_id or ad_id in ("", "0", "0.0"):
            print(f"[{i+1}/{total}] SKIP (no Ad ID): {ad_name}")
            transkripts.update_cell(sheet_row, col_transcript + 1, "[No Ad ID found]")
            continue

        print(f"[{i+1}/{total}] {channel} / {media_type}: {ad_name}")

        transcript = ""
        try:
            if channel == "Meta":
                if "video" in media_type.lower():
                    transcript = transcribe_meta_video(ad_id, oai)
                else:
                    transcript = transcribe_meta_image(ad_id, oai)

            elif channel == "Youtube":
                if ga_service:
                    transcript = transcribe_youtube(ad_id, ga_service, CUSTOMER_ID)
                else:
                    transcript = "[Google Ads client unavailable]"

            elif channel == "Display":
                if ga_service:
                    transcript = transcribe_display(ad_id, ga_service, CUSTOMER_ID, oai)
                else:
                    transcript = "[Google Ads client unavailable]"

            else:
                transcript = f"[Unknown channel: {channel}]"

        except Exception as e:
            transcript = f"[ERROR: {e}]"
            errors += 1
            print(f"  ERROR: {e}")

        cell_value = transcript if transcript else "[No content returned]"
        pending_writes[sheet_row] = cell_value
        print(f"  ✓ Queued ({len(cell_value)} chars)")
        done += 1

        # Flush every 20 rows to avoid losing everything on a crash
        if len(pending_writes) >= 20:
            _flush(transkripts, pending_writes, col_transcript)

    _flush(transkripts, pending_writes, col_transcript)
    print(f"\nDone. {done} processed, {errors} errors.")


if __name__ == "__main__":
    main()
