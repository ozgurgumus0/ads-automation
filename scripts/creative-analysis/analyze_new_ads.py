"""
Self-contained Ad Details enricher.

Reads the 'Ad Details' tab, finds rows where the Gemini-derived columns
(Hook category, Hook text, POV, Setting, Edit pace, Format & duration,
Person on screen, Main message) are blank, and fills them.

- Any cell the user has already populated by hand is preserved (never overwritten).
- Format & duration + Edit pace come from ffmpeg locally (exact, free).
- Hook fields come from Gemini 2.5 Flash Lite.
- Idempotent via analysis_cache.jsonl. Re-running only processes new rows.

Usage:
    python analyze_new_ads.py                # process every empty row
    python analyze_new_ads.py --limit 10     # do 10 then stop (testing)
    python analyze_new_ads.py --refresh AD   # force re-analyze a specific ad
"""
import argparse, json, os, time, sys
from pathlib import Path

import gspread
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / "ads_automation" / ".env")

# Reuse the helpers — they're well-tested.
sys.path.insert(0, str(HERE))
from analyze_creatives import (
    OUTPUT_COLS, SHEET_ID, SA_PATH, MEDIA_ROOT, CACHE_PATH,
    safe, normalize_ad_name, find_media,
    probe_image, probe_video, detect_edit_pace, extract_keyframes,
    init_gemini, analyze_image_with_gemini, analyze_video_with_gemini,
    load_cache, append_cache, MIN_INTERVAL_SEC,
)
import imageio_ffmpeg

GEMINI_COLS = ["Hook category", "Hook text", "POV", "Setting",
               "Edit pace", "Format & duration", "Person on screen", "Main message"]


def needs_filling(row: dict) -> bool:
    """A row needs Gemini analysis if Hook category is empty (the 'anchor' field)."""
    return not (row.get("Hook category") or "").strip()


def get_gc():
    return gspread.service_account(filename=str(SA_PATH))


def read_ad_details(gc):
    ws = gc.open_by_key(SHEET_ID).worksheet("Ad Details")
    vals = ws.get_all_values()
    if not vals:
        return ws, [], []
    header = vals[0]
    rows = []
    for r in vals[1:]:
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        rows.append(rec)
    return ws, header, rows


def push_ad_details(ws, header, rows):
    table = [header] + [[r.get(c, "") for c in header] for r in rows]
    needed = len(table)
    if ws.row_count < needed:
        ws.add_rows(needed - ws.row_count + 100)
    ws.clear()
    ws.update(values=table, range_name="A1", value_input_option="USER_ENTERED")
    print(f"Wrote {len(rows)} rows to 'Ad Details'")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--refresh", action="append", default=[],
                   help="Ad Name(s) to re-analyze even if filled (can repeat)")
    p.add_argument("--force", action="store_true",
                   help="Re-analyze every row that has media (overwrites H-O even if filled). "
                        "Use after a prompt change or taxonomy update.")
    args = p.parse_args()

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cache = load_cache()
    print(f"cache: {len(cache)} rows")

    gc = get_gc()
    ws, header, rows = read_ad_details(gc)
    print(f"Ad Details: {len(rows)} rows")

    # Index media
    vids      = {p.stem.lower(): p for p in (MEDIA_ROOT / "videos").glob("*.mp4")}
    vids_orph = {p.stem.lower(): p for p in (MEDIA_ROOT / "videos" / "_orphans").glob("*.mp4")}
    imgs      = {}
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for p in (MEDIA_ROOT / "images").glob(ext):
            imgs[p.stem.lower()] = p
    print(f"media: {len(vids)} videos, {len(vids_orph)} orphans, {len(imgs)} images")

    refresh = {n.strip() for n in args.refresh if n.strip()}
    if args.force:
        todo = [r for r in rows if r.get("Ad Name", "").strip()]
    else:
        todo = [r for r in rows if (
            r.get("Ad Name", "").strip() and
            (needs_filling(r) or r["Ad Name"].strip() in refresh)
        )]
    print(f"{len(todo)} rows need analysis\n")

    if not todo:
        return

    client = init_gemini()
    last_call = 0.0
    n_ok = n_fail = n_nomedia = 0

    for i, row in enumerate(todo, 1):
        ad_name = row["Ad Name"].strip()
        media_path, kind = find_media(ad_name, vids, vids_orph, imgs)
        if not media_path:
            n_nomedia += 1
            print(f"  [{i}/{len(todo)}] {ad_name[:60]}: NO MEDIA")
            continue

        if kind == "video":
            fmt, dur_s = probe_video(media_path, ffmpeg_exe)
            edit_pace = detect_edit_pace(media_path, ffmpeg_exe, dur_s)
            frames = extract_keyframes(media_path, ffmpeg_exe, dur_s, n=6)
        else:
            fmt = probe_image(media_path); edit_pace = "static"; frames = []

        wait = MIN_INTERVAL_SEC - (time.time() - last_call)
        if wait > 0: time.sleep(wait)

        try:
            if kind == "video":
                if not frames: raise RuntimeError("no keyframes")
                res = analyze_video_with_gemini(
                    client, frames, fmt.split(", ", 1)[1], row.get("Transcript", "")
                )
            else:
                res = analyze_image_with_gemini(client, media_path)
            last_call = time.time()
        except Exception as e:
            err = str(e)
            n_fail += 1
            print(f"  [{i}/{len(todo)}] {ad_name[:60]}: FAILED -- {err[:200]}")
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                print("    rate-limited, sleeping 60s")
                time.sleep(60)
            continue

        # Map Gemini fields to sheet column names
        gemini_to_col = {
            "hook_category":    "Hook category",
            "hook_text":        "Hook text",
            "pov":              "POV",
            "setting":          "Setting",
            "person_on_screen": "Person on screen",
            "main_message":     "Main message",
        }
        # By default, only fill cells that are currently blank (preserve any manual edits).
        # With --force, overwrite all Gemini-derived fields (used after prompt/taxonomy change).
        for k_gem, k_col in gemini_to_col.items():
            if args.force or not (row.get(k_col) or "").strip():
                row[k_col] = res.get(k_gem, "")
        if args.force or not (row.get("Edit pace") or "").strip():
            row["Edit pace"] = edit_pace
        if args.force or not (row.get("Format & duration") or "").strip():
            row["Format & duration"] = fmt

        # Append to cache for resumability
        full_cached = {col: row.get(col, "") for col in OUTPUT_COLS}
        append_cache(full_cached)
        cache[ad_name] = full_cached
        n_ok += 1
        print(f"  [{i}/{len(todo)}] {ad_name[:60]}  -- {fmt}")

        # Periodic push every 30 successful analyses
        if n_ok and n_ok % 30 == 0:
            push_ad_details(ws, header, rows)

        if args.limit and n_ok >= args.limit:
            print(f"--limit {args.limit} reached")
            break

    push_ad_details(ws, header, rows)
    print(f"\nDone. analyzed={n_ok}, no-media={n_nomedia}, failed={n_fail}")


if __name__ == "__main__":
    main()
