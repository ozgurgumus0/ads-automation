"""
Fill all 15 columns of 'Ad Details' for ads listed in 'important_rows' but missing
from 'Ad Details'. Source of everything is the Meta API (creative spec for ad copy,
adimages/advideos for media) plus Gemini for taxonomy + transcript/OCR.

Steps per ad:
  1. Look up local media (already-downloaded). If absent, try to download from Meta.
  2. If we have media:
       - probe Format & duration / Media Type
       - generate Transcript (Gemini audio for video; OCR for image)
       - fetch Description (Headline + Body from Meta creative spec)
       - run Gemini taxonomy (hook_category, hook_text, pov, setting,
         person_on_screen, main_message)
       - derive Product Type, Promo, edit pace
  3. Append a new row to 'Ad Details'.

Idempotent: appends to analysis_cache.jsonl and skips ads already in Ad Details.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv
from PIL import Image as PILImage

HERE = Path(__file__).parent
load_dotenv(HERE / "ads_automation" / ".env")
sys.path.insert(0, str(HERE))

from analyze_creatives import (
    SHEET_ID, SA_PATH, MEDIA_ROOT, CACHE_PATH, OUTPUT_COLS,
    safe, normalize_ad_name,
    probe_image, probe_video, detect_edit_pace, extract_keyframes,
    init_gemini, analyze_image_with_gemini, analyze_video_with_gemini,
    load_cache, append_cache,
    GEMINI_MODEL,
)

GRAPH = "https://graph.facebook.com"
META_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_VERSION = os.getenv("META_API_VERSION", "v21.0")
META_ACCOUNT = os.getenv("META_AD_ACCOUNT_ID", "").lstrip("act_")

DETAILS_TAB = "Ad Details"
IMPORTANT_TAB = "important_rows"

ASSET_MAP_PATH = MEDIA_ROOT / "_asset_maps.json"
AD_COPY_CACHE = MEDIA_ROOT / "_ad_copy.json"


# ----- ffmpeg discovery (mirror of analyze_creatives behaviour) -----
def find_ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ----- Meta API helpers -----
def graph_get(path: str, params: dict, version: str = META_VERSION) -> dict:
    url = f"{GRAPH}/{version}/{path}"
    for attempt in range(1, 6):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 30 * attempt
            print(f"    transient {r.status_code}, retry in {wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"GET {url} -> {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"giving up on {url}")


def stream_download(url: str, dest: Path) -> int:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    tmp.rename(dest)
    return dest.stat().st_size


def fetch_video_source(video_id: str) -> str | None:
    try:
        d = graph_get(video_id, {"access_token": META_TOKEN, "fields": "source"})
        return d.get("source")
    except Exception as e:
        print(f"    video {video_id}: {e}")
        return None


def fetch_image_url(image_hash: str) -> str | None:
    """Fetch image URL by hash from the ad account's adimages."""
    try:
        d = graph_get(
            f"act_{META_ACCOUNT}/adimages",
            {"access_token": META_TOKEN, "fields": "url,permalink_url",
             "hashes": json.dumps([image_hash])},
        )
        items = d.get("data", [])
        if items:
            return items[0].get("url")
    except Exception as e:
        print(f"    image {image_hash}: {e}")
    return None


def download_for_ad(ad_name: str, asset_map: dict) -> tuple[Path | None, str | None]:
    """Download the primary creative for ad_name. Returns (path, kind) or (None, None)."""
    entry = asset_map.get(ad_name)
    if not entry:
        return None, None
    base = safe(ad_name)
    # Prefer videos when available
    for vid in entry.get("videos", []):
        src = fetch_video_source(vid)
        if not src:
            continue
        ext = "mp4"
        dest_dir = MEDIA_ROOT / "videos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{base}.{ext}"
        if dest.exists():
            return dest, "video"
        try:
            sz = stream_download(src, dest)
            print(f"    downloaded video {dest.name} ({sz/1024/1024:.1f} MB)")
            # update video ledger
            ledger_path = dest_dir / "_downloaded.json"
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
            except Exception:
                ledger = {}
            ledger[vid] = dest.name
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            return dest, "video"
        except Exception as e:
            print(f"    failed download {vid}: {e}")
            continue
    # Fall back to images
    for h in entry.get("images", []):
        url = fetch_image_url(h)
        if not url:
            continue
        ext = "jpg"
        dest_dir = MEDIA_ROOT / "images"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{base}.{ext}"
        if dest.exists():
            return dest, "image"
        try:
            sz = stream_download(url, dest)
            print(f"    downloaded image {dest.name} ({sz/1024:.1f} KB)")
            return dest, "image"
        except Exception as e:
            print(f"    failed download {h}: {e}")
            continue
    return None, None


# ----- Ad copy extraction (Description column) -----
def build_ad_copy_index() -> dict:
    """{ad_name -> {headline, body, message, link_caption}} via /act/ads."""
    if AD_COPY_CACHE.exists():
        try:
            return json.loads(AD_COPY_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    print("Building ad-copy index from /act/ads ...")
    out = {}
    fields = (
        "name,creative{"
        "object_story_spec,asset_feed_spec,"
        "title,body,link_url"
        "}"
    )
    url = f"{GRAPH}/{META_VERSION}/act_{META_ACCOUNT}/ads"
    params = {"access_token": META_TOKEN, "fields": fields, "limit": 100}
    next_url = None
    n = 0
    while True:
        if next_url:
            r = requests.get(next_url, timeout=60)
        else:
            r = requests.get(url, params=params, timeout=60)
        if r.status_code != 200:
            print(f"  /ads query failed: {r.status_code} {r.text[:200]}")
            break
        data = r.json()
        for ad in data.get("data", []):
            name = ad.get("name") or ""
            if not name:
                continue
            cr = ad.get("creative") or {}
            headline = body = message = link_caption = ""
            # Direct creative fields
            headline = cr.get("title") or ""
            body = cr.get("body") or ""
            # link_data inside object_story_spec
            oss = cr.get("object_story_spec") or {}
            ld = oss.get("link_data") or {}
            headline = headline or ld.get("name", "")
            body = body or ld.get("message", "")
            message = ld.get("description") or ""
            link_caption = ld.get("caption") or ""
            # video_data
            vd = oss.get("video_data") or {}
            headline = headline or vd.get("title", "")
            body = body or vd.get("message", "")
            # asset_feed_spec dynamic creative
            afs = cr.get("asset_feed_spec") or {}
            if not headline and afs.get("titles"):
                headline = (afs["titles"][0] or {}).get("text", "")
            if not body and afs.get("bodies"):
                body = (afs["bodies"][0] or {}).get("text", "")
            if not message and afs.get("descriptions"):
                message = (afs["descriptions"][0] or {}).get("text", "")
            if headline or body:
                # Keep the longest variant if duplicates exist
                prev = out.get(name)
                cur_len = len((headline or "") + (body or ""))
                prev_len = len((prev or {}).get("headline", "") + (prev or {}).get("body", "")) if prev else -1
                if cur_len > prev_len:
                    out[name] = {
                        "headline": headline.strip(),
                        "body": body.strip(),
                        "message": (message or "").strip(),
                        "link_caption": (link_caption or "").strip(),
                    }
            n += 1
        next_url = (data.get("paging") or {}).get("next")
        if not next_url:
            break
    print(f"  scanned {n} ads, captured copy for {len(out)} unique names")
    AD_COPY_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def format_description(copy: dict) -> str:
    if not copy:
        return ""
    parts = []
    if copy.get("headline"): parts.append(f"Headline: {copy['headline']}")
    if copy.get("body"):     parts.append(f"Body: {copy['body']}")
    if copy.get("message"):  parts.append(f"Description: {copy['message']}")
    return "\n".join(parts)


# ----- Gemini transcript / OCR -----
TRANSCRIBE_VIDEO_PROMPT = """Transcribe the spoken Italian audio in this video.
Return one line per spoken segment, in the format:
[MM:SS] testo parlato
If no spoken audio, return: (nessun parlato)
Output ONLY the transcript lines, no preamble.
"""

OCR_IMAGE_PROMPT = """Extract every readable line of text from this Italian advertising image
in reading order (top to bottom, left to right). Return one line per visible text element.
Do NOT add commentary. If there is no visible text, return: (nessun testo)
"""


def transcribe_video(client, video_path: Path) -> str:
    from google.genai import types
    with open(video_path, "rb") as f:
        data = f.read()
    part = types.Part.from_bytes(data=data, mime_type="video/mp4")
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[part, TRANSCRIBE_VIDEO_PROMPT],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    return (resp.text or "").strip()


def ocr_image(client, image_path: Path) -> str:
    from google.genai import types
    with open(image_path, "rb") as f:
        data = f.read()
    ext = image_path.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "image/jpeg")
    part = types.Part.from_bytes(data=data, mime_type=mime)
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[part, OCR_IMAGE_PROMPT],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    return (resp.text or "").strip()


# ----- Derived columns -----
def derive_product_type(ad_name: str) -> str:
    # All current product families are PV (photovoltaic)
    return "PV"


def derive_promo(ad_name: str) -> str:
    # Match the convention used in existing rows: extract prefix up to the first "."
    nm = ad_name.strip()
    for prefix in ("PROMO_A_Brand", "PROMO_B_Brand", "Ecobonus", "SliderBonus"):
        if nm.startswith(prefix):
            return prefix
    return nm.split(".")[0]


# ----- Asset map → reverse index -----
def load_asset_map() -> dict:
    """Returns {ad_name: {videos: [unique vid_ids], images: [unique hashes]}}."""
    raw = json.loads(ASSET_MAP_PATH.read_text(encoding="utf-8"))
    out = {}
    for h, names in raw["img_to_ads"].items():
        for n in names:
            out.setdefault(n, {"images": [], "videos": []})
            if h not in out[n]["images"]:
                out[n]["images"].append(h)
    for v, names in raw["vid_to_ads"].items():
        for n in names:
            out.setdefault(n, {"images": [], "videos": []})
            if v not in out[n]["videos"]:
                out[n]["videos"].append(v)
    return out


# ----- Local media lookup -----
def build_local_indexes() -> tuple[dict, dict]:
    vroot = MEDIA_ROOT / "videos"
    iroot = MEDIA_ROOT / "images"
    vids = {safe(p.stem).lower(): p for p in vroot.rglob("*") if p.is_file()} if vroot.exists() else {}
    imgs = {safe(p.stem).lower(): p for p in iroot.rglob("*") if p.is_file()} if iroot.exists() else {}
    return vids, imgs


# ----- Main loop -----
def process_ad(ad_name: str, channel: str, asset_map: dict, ad_copy: dict,
               vids: dict, imgs: dict, gemini, ffmpeg_exe: str) -> dict | None:
    print(f"\n=== {ad_name} ({channel}) ===")
    s = safe(ad_name).lower()
    media_path: Path | None = None
    media_kind: str | None = None
    if s in vids:
        media_path, media_kind = vids[s], "video"
        print(f"  local video: {media_path.name}")
    elif s in imgs:
        media_path, media_kind = imgs[s], "image"
        print(f"  local image: {media_path.name}")
    else:
        # Try Meta download
        media_path, media_kind = download_for_ad(ad_name, asset_map)
        if media_path:
            # refresh local index for next iterations
            if media_kind == "video":
                vids[s] = media_path
            else:
                imgs[s] = media_path

    if not media_path:
        print(f"  NO MEDIA — skipping")
        return None

    # Probe + media-type
    if media_kind == "video":
        fmt_dur, dur_s = probe_video(media_path, ffmpeg_exe)
        edit_pace = detect_edit_pace(media_path, ffmpeg_exe, dur_s)
    else:
        fmt_dur = probe_image(media_path)
        dur_s = 0.0
        edit_pace = "static"
    media_type = "Video" if media_kind == "video" else "Image"

    # Transcript
    print(f"  transcribing ...")
    try:
        if media_kind == "video":
            transcript = transcribe_video(gemini, media_path)
        else:
            transcript = ocr_image(gemini, media_path)
    except Exception as e:
        print(f"    transcript error: {e}")
        transcript = ""

    # Description = ad copy from Meta
    copy = ad_copy.get(ad_name) or {}
    description = format_description(copy)

    # Taxonomy
    print(f"  taxonomy ...")
    try:
        if media_kind == "video":
            frames = extract_keyframes(media_path, ffmpeg_exe, dur_s, n=6)
            tax = analyze_video_with_gemini(gemini, frames, fmt_dur, transcript)
        else:
            tax = analyze_image_with_gemini(gemini, media_path)
    except Exception as e:
        print(f"    taxonomy error: {e}")
        return None

    row = {
        "Ad Name":            ad_name,
        "Product Type":       derive_product_type(ad_name),
        "Channel":            channel,
        "Media Type":         media_type,
        "Promo":              derive_promo(ad_name),
        "Description":        description,
        "Transcript":         transcript,
        "Hook category":      tax.get("hook_category", ""),
        "Hook text":          tax.get("hook_text", ""),
        "POV":                tax.get("pov", ""),
        "Setting":             tax.get("setting", ""),
        "Edit pace":          edit_pace,
        "Format & duration":  fmt_dur,
        "Person on screen":   tax.get("person_on_screen", ""),
        "Main message":       tax.get("main_message", ""),
    }
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="Process at most N ads (0=all)")
    p.add_argument("--dry-run", action="store_true", help="Compute rows but don't append to sheet")
    p.add_argument("--rebuild-copy", action="store_true", help="Rebuild ad-copy index from Meta")
    p.add_argument("--only", help="Process only ads with names matching this substring")
    args = p.parse_args()

    if args.rebuild_copy and AD_COPY_CACHE.exists():
        AD_COPY_CACHE.unlink()

    if not META_TOKEN or not META_ACCOUNT:
        sys.exit("META_ACCESS_TOKEN / META_AD_ACCOUNT_ID missing in ads_automation/.env")

    ffmpeg_exe = find_ffmpeg()

    gc = gspread.service_account(filename=str(SA_PATH))
    sh = gc.open_by_key(SHEET_ID)
    imp = sh.worksheet(IMPORTANT_TAB).get_all_values()
    det_ws = sh.worksheet(DETAILS_TAB)
    det = det_ws.get_all_values()

    # Find col indexes
    def col(h, n):
        for i, x in enumerate(h):
            if x.strip().lower() == n.lower():
                return i
        return -1

    n_idx = col(imp[0], "Ad Name")
    c_idx = col(imp[0], "Channel")

    # Existing names in Ad Details (lowercase, both raw and normalized)
    existing = set()
    det_name_idx = col(det[0], "Ad Name")
    for r in det[1:]:
        if det_name_idx < len(r):
            nm = (r[det_name_idx] or "").strip()
            if nm:
                existing.add(nm.lower())
                existing.add(normalize_ad_name(nm).lower())

    # Build the to-do list (deduplicated, in important_rows order)
    todo = []
    seen = set()
    for r in imp[1:]:
        nm = (r[n_idx] or "").strip()
        if not nm or nm in seen:
            continue
        if nm.lower() in existing or normalize_ad_name(nm).lower() in existing:
            continue
        seen.add(nm)
        chan = (r[c_idx] or "").strip() if c_idx < len(r) else ""
        todo.append((nm, chan))

    print(f"To-do: {len(todo)} ads missing from Ad Details")
    if args.only:
        before = len(todo)
        todo = [(n, c) for (n, c) in todo if args.only.lower() in n.lower()]
        print(f"  --only '{args.only}': {len(todo)}/{before} match")
    if args.limit:
        todo = todo[: args.limit]
        print(f"  (limited to first {args.limit})")

    asset_map = load_asset_map()
    ad_copy = build_ad_copy_index()
    vids, imgs = build_local_indexes()
    gemini = init_gemini()
    cache = load_cache()

    new_rows = []
    skipped = []
    for nm, chan in todo:
        if nm in cache:
            print(f"\n=== {nm} (cached) ===")
            row = cache[nm]
        else:
            row = process_ad(nm, chan, asset_map, ad_copy, vids, imgs, gemini, ffmpeg_exe)
            if row is None:
                skipped.append((nm, chan))
                continue
            append_cache(row)
        new_rows.append(row)

    print(f"\n=== Summary ===")
    print(f"  Filled: {len(new_rows)}")
    print(f"  Skipped (no media available): {len(skipped)}")
    for nm, chan in skipped:
        print(f"    - {nm} [{chan}]")

    if not new_rows:
        return
    if args.dry_run:
        print("\n--dry-run: not writing to sheet")
        return

    # Append to sheet, in OUTPUT_COLS order
    header = det[0]
    # Map our row dict to the sheet's column order
    rows_to_append = []
    for row in new_rows:
        rows_to_append.append([row.get(h, "") for h in header])
    print(f"\nAppending {len(rows_to_append)} rows to '{DETAILS_TAB}' ...")
    det_ws.append_rows(rows_to_append, value_input_option="RAW")
    print("Done.")


if __name__ == "__main__":
    main()
