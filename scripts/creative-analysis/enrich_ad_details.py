"""
Merge Descriptions tab into Ad Details:

- Update Promo (and richer Description) on existing Ad Details rows where the ad
  name matches an entry in Descriptions (direct match OR via the
  '(real_name) anunț video generat automat' wrapper pattern).

- Append every ad that is in Descriptions but not yet in Ad Details, running the
  same Gemini analysis on its media if we have a file. Ads with no downloadable
  media get the columns A-G filled from Descriptions/Transkripts and H-O blank.

Re-uses analysis_cache.jsonl from the prior run; new analyses are appended.
"""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

import gspread
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / "ads_automation" / ".env")

# Reuse all the helpers from analyze_creatives
sys.path.insert(0, str(HERE))
from analyze_creatives import (
    OUTPUT_COLS, SHEET_ID, SA_PATH, MEDIA_ROOT, CACHE_PATH,
    safe, normalize_ad_name, find_media,
    probe_image, probe_video, detect_edit_pace, extract_keyframes,
    init_gemini, analyze_image_with_gemini, analyze_video_with_gemini,
    load_cache, append_cache,
    MIN_INTERVAL_SEC,
)
import imageio_ffmpeg


def get_gc():
    return gspread.service_account(filename=str(SA_PATH))


def read_tab(gc, tab_name: str) -> tuple[list[str], list[dict]]:
    ws = gc.open_by_key(SHEET_ID).worksheet(tab_name)
    rows = ws.get_all_values()
    if not rows:
        return [], []
    header = rows[0]
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        out.append(rec)
    return header, out


def lookup_key(name: str) -> str:
    """Normalize a name for matching across tabs (handles the '(inner) anunț...' wrapper)."""
    inner = normalize_ad_name(name)
    return inner.strip().lower()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after analyzing N new ads (testing)")
    p.add_argument("--no-analyze", action="store_true",
                   help="Skip Gemini calls — just merge existing data and push to sheet")
    args = p.parse_args()

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    print("Loading cache...")
    cache = load_cache()
    print(f"  {len(cache)} ads already analyzed")

    print("Reading Descriptions, Transkripts, Ad Details...")
    gc = get_gc()
    _, descs    = read_tab(gc, "Descriptions")
    _, transk   = read_tab(gc, "Transkripts")
    _, details  = read_tab(gc, "Ad Details")
    print(f"  Descriptions: {len(descs)}, Transkripts: {len(transk)}, Ad Details: {len(details)}")

    # Index Descriptions by lookup_key, keep first occurrence
    desc_by_key = {}
    for d in descs:
        k = lookup_key(d.get("Ad Name", ""))
        if k and k not in desc_by_key:
            desc_by_key[k] = d

    # Index Transkripts by lookup_key
    transk_by_key = {}
    for t in transk:
        k = lookup_key(t.get("Ad Name", ""))
        if k and k not in transk_by_key:
            transk_by_key[k] = t

    # Index existing Ad Details (current cache rows = analysis_cache) by lookup_key
    cache_by_key = {lookup_key(name): row for name, row in cache.items()}

    # ---- Index media files ----
    print("Indexing media files...")
    vids      = {p.stem.lower(): p for p in (MEDIA_ROOT / "videos").glob("*.mp4")}
    vids_orph = {p.stem.lower(): p for p in (MEDIA_ROOT / "videos" / "_orphans").glob("*.mp4")}
    imgs      = {}
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for p in (MEDIA_ROOT / "images").glob(ext):
            imgs[p.stem.lower()] = p
    print(f"  {len(vids)} videos, {len(vids_orph)} orphan videos, {len(imgs)} images")

    # ---- Final ordered ad-name list ----
    # Order: Descriptions order first (632), then any Ad-Details-only ads (33) appended.
    final_order = []
    seen = set()
    for d in descs:
        name = d.get("Ad Name", "").strip()
        if not name:
            continue
        k = lookup_key(name)
        if k in seen:
            continue
        seen.add(k)
        final_order.append(("desc", name, k))
    for det in details:
        name = det.get("Ad Name", "").strip()
        if not name:
            continue
        k = lookup_key(name)
        if k in seen:
            continue
        seen.add(k)
        final_order.append(("detail-only", name, k))

    print(f"\n{len(final_order)} unique ads to write to Ad Details")

    # ---- Gemini client (lazy) ----
    client = None
    if not args.no_analyze:
        client = init_gemini()
    last_call = 0.0

    out_rows = []
    n_analyzed = n_skip = n_nomedia = n_fail = 0

    for i, (origin, raw_name, key) in enumerate(final_order, 1):
        d = desc_by_key.get(key)
        t = transk_by_key.get(key)
        c = cache_by_key.get(key)

        # ---- Pull A-G data with sensible fallbacks ----
        ad_name = (d or t or {}).get("Ad Name") or raw_name
        # Prefer Transkripts for Product Type/Channel/Media Type/Transcript (it's our richest source)
        product_type = (t or {}).get("Product Type", "") or (c or {}).get("Product Type", "")
        channel      = (t or {}).get("Channel", "")      or (c or {}).get("Channel", "")
        media_type   = (t or {}).get("Media Type", "")   or (c or {}).get("Media Type", "")
        if not media_type and d:
            media_type = d.get("Formato", "").capitalize()
        # Promo: prefer Descriptions
        promo        = (d or {}).get("Promo", "") or (c or {}).get("Promo", "") or (t or {}).get("Promo", "")
        # Description: prefer the existing cache (Gemini-fed Italian) if present, else
        # build from Descriptions intro + descrizione, else Transkripts.
        if c and c.get("Description"):
            description = c["Description"]
        elif d and (d.get("Intro video") or d.get("Descrizione video")):
            parts = []
            if d.get("Intro video"):     parts.append(d["Intro video"].strip())
            if d.get("Descrizione video"): parts.append(d["Descrizione video"].strip())
            description = "\n\n".join(parts)
        else:
            description = (t or {}).get("Description", "") or (c or {}).get("Description", "")
        transcript   = (c or {}).get("Transcript", "") or (t or {}).get("Transcript", "")

        # ---- H-O: from cache, or run Gemini if we have media ----
        H = c.get("Hook category", "") if c else ""
        I = c.get("Hook text", "") if c else ""
        J = c.get("POV", "") if c else ""
        K = c.get("Setting", "") if c else ""
        L = c.get("Edit pace", "") if c else ""
        M = c.get("Format & duration", "") if c else ""
        N = c.get("Person on screen", "") if c else ""
        O = c.get("Main message", "") if c else ""

        needs_analysis = not c and not args.no_analyze
        if needs_analysis:
            media_path, kind = find_media(ad_name, vids, vids_orph, imgs)
            if not media_path:
                n_nomedia += 1
                # write whatever we have, leave H-O blank
            else:
                if kind == "video":
                    fmt, dur_s = probe_video(media_path, ffmpeg_exe)
                    edit_pace = detect_edit_pace(media_path, ffmpeg_exe, dur_s)
                    frames = extract_keyframes(media_path, ffmpeg_exe, dur_s, n=6)
                else:
                    fmt = probe_image(media_path); edit_pace = "static"; frames = []
                # throttle
                wait = MIN_INTERVAL_SEC - (time.time() - last_call)
                if wait > 0:
                    time.sleep(wait)
                try:
                    if kind == "video":
                        if not frames:
                            raise RuntimeError("no keyframes")
                        res = analyze_video_with_gemini(client, frames, fmt.split(", ", 1)[1], transcript)
                    else:
                        res = analyze_image_with_gemini(client, media_path)
                    last_call = time.time()
                    n_analyzed += 1
                    H = res.get("hook_category", "")
                    I = res.get("hook_text", "")
                    J = res.get("pov", "")
                    K = res.get("setting", "")
                    L = edit_pace
                    M = fmt
                    N = res.get("person_on_screen", "")
                    O = res.get("main_message", "")
                    # Append to disk cache for resumability
                    full_cached = {
                        "Ad Name": ad_name, "Product Type": product_type, "Channel": channel,
                        "Media Type": media_type, "Promo": promo, "Description": description,
                        "Transcript": transcript,
                        "Hook category": H, "Hook text": I, "POV": J, "Setting": K,
                        "Edit pace": L, "Format & duration": M,
                        "Person on screen": N, "Main message": O,
                    }
                    append_cache(full_cached)
                    cache[ad_name] = full_cached
                    cache_by_key[key] = full_cached
                    print(f"  [{i}/{len(final_order)}] {ad_name[:60]} -- {kind}, {fmt}")
                except Exception as e:
                    err = str(e)
                    print(f"  [{i}/{len(final_order)}] {ad_name[:60]}: FAILED -- {err[:200]}")
                    n_fail += 1
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        print("    rate-limited, sleeping 60s")
                        time.sleep(60)
        else:
            n_skip += 1
            if i % 100 == 0:
                print(f"  [{i}/{len(final_order)}] (cached/no-analyze)")

        # Soggetto from Descriptions wins over the Gemini-derived person_on_screen.
        if d and (d.get("Soggetto") or "").strip():
            N = d["Soggetto"].strip()

        out_rows.append({
            "Ad Name":            ad_name,
            "Product Type":       product_type,
            "Channel":            channel,
            "Media Type":         media_type,
            "Promo":              promo,
            "Description":        description,
            "Transcript":         transcript,
            "Hook category":      H,
            "Hook text":          I,
            "POV":                J,
            "Setting":            K,
            "Edit pace":          L,
            "Format & duration":  M,
            "Person on screen":   N,
            "Main message":       O,
        })

        # Periodic push every 50 fresh analyses
        if n_analyzed and n_analyzed % 50 == 0:
            push_to_sheet(gc, out_rows)

        if args.limit and n_analyzed >= args.limit:
            print(f"--limit {args.limit} reached")
            # stop processing further analyses but keep emitting the rest as-is
            args.no_analyze = True

    # Final push
    push_to_sheet(gc, out_rows)
    print(f"\nDone. analyzed={n_analyzed}, cached/no-analyze={n_skip}, no-media={n_nomedia}, failed={n_fail}")
    print(f"Total rows in Ad Details: {len(out_rows)}")


def push_to_sheet(gc, rows: list[dict]):
    ws = gc.open_by_key(SHEET_ID).worksheet("Ad Details")
    table = [OUTPUT_COLS] + [[r.get(c, "") for c in OUTPUT_COLS] for r in rows]
    ws.clear()
    # Make sure the sheet has enough rows
    needed = len(table)
    if ws.row_count < needed:
        ws.add_rows(needed - ws.row_count + 100)
    ws.update(values=table, range_name="A1", value_input_option="USER_ENTERED")
    print(f"Wrote {len(rows)} rows to 'Ad Details'")


if __name__ == "__main__":
    main()
