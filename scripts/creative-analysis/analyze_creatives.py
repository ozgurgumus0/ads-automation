"""
Fill the "Ad Details" tab of the creative-ad-performance Google Sheet.

Source columns A-G (Ad Name, Product Type, Channel, Media Type, Promo,
Description, Transcript) come from the existing "Transkripts" tab.

Columns H-O are filled by Gemini 2.0 Flash for each creative:
  H  Hook category
  I  Hook text
  J  POV
  K  Setting
  L  Edit pace
  M  Format & duration  (computed locally from the file, not Gemini)
  N  Person on screen
  O  Main message

Resumable: appends to analysis_cache.jsonl as it goes; rerun to continue.

Usage:
    python analyze_creatives.py                     # process everything not yet cached
    python analyze_creatives.py --limit 10          # do 10 then stop (for testing)
    python analyze_creatives.py --push-only         # don't call Gemini, just push cache to sheet
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import gspread
from dotenv import load_dotenv
from PIL import Image

HERE = Path(__file__).parent
load_dotenv(HERE / "ads_automation" / ".env")

SHEET_ID = "__SHEET_ID__"
SOURCE_TAB = "Transkripts"
TARGET_TAB = "Ad Details"
SA_PATH = HERE / "ads_automation" / "service_account.json"
MEDIA_ROOT = HERE / "meta_creatives" / "__META_ACCOUNT_ID__"
CACHE_PATH = HERE / "analysis_cache.jsonl"

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Paid tier — minimal throttle to be a good API citizen.
MIN_INTERVAL_SEC = 0.3

OUTPUT_COLS = [
    "Ad Name", "Product Type", "Channel", "Media Type", "Promo",
    "Description", "Transcript",
    "Hook category", "Hook text", "POV", "Setting", "Edit pace",
    "Format & duration", "Person on screen", "Main message",
]

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe(name: str, max_len: int = 80) -> str:
    s = SAFE.sub("_", (name or "").strip()).strip("._")
    return (s[:max_len] or "untitled")


def normalize_ad_name(raw: str) -> str:
    """Some Transkripts ad names are wrapped:  '(real_name) anunț video generat automat'.
    Strip that wrapper and return the inner name."""
    raw = (raw or "").strip()
    m = re.match(r"^\(([^)]+)\)\s*anun[țt]\s*video", raw, re.IGNORECASE)
    return m.group(1).strip() if m else raw


def find_media(ad_name: str, vids: dict, vids_orph: dict, imgs: dict):
    """Return (path, kind in {'video','image'}) or (None, None)."""
    candidates = [ad_name, normalize_ad_name(ad_name)]
    for c in candidates:
        s = safe(c).lower()
        if not s:
            continue
        if s in vids:      return vids[s], "video"
        if s in vids_orph: return vids_orph[s], "video"
        if s in imgs:      return imgs[s], "image"
    return None, None


def aspect_label(w: int, h: int) -> str:
    if not w or not h:
        return "?"
    r = w / h
    candidates = {"9:16": 9/16, "1:1": 1.0, "4:5": 4/5, "16:9": 16/9, "4:3": 4/3, "2:1": 2.0}
    best = min(candidates.items(), key=lambda kv: abs(kv[1] - r))
    if abs(best[1] - r) / best[1] < 0.05:
        return best[0]
    return f"{w}x{h}"


def duration_bucket(dur_s: float) -> str:
    if dur_s <= 0:    return "image"
    if dur_s <= 15:   return "short"
    if dur_s <= 30:   return "medium"
    return "long"


def probe_image(path: Path) -> str:
    try:
        with Image.open(path) as im:
            w, h = im.size
        return f"image, {aspect_label(w, h)}, image"
    except Exception:
        return "image, ?, image"


def probe_video(path: Path, ffmpeg_exe: str) -> tuple[str, float]:
    """Return ('video, 9:16, 0:23, medium', duration_seconds)."""
    try:
        proc = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=30,
        )
        err = proc.stderr or ""
        dur_m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
        dim_m = re.search(r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})", err)
        total_s = 0.0
        if dur_m:
            h, m, s = int(dur_m.group(1)), int(dur_m.group(2)), float(dur_m.group(3))
            total_s = h * 3600 + m * 60 + s
        if dim_m:
            w, h = int(dim_m.group(1)), int(dim_m.group(2))
            ar = aspect_label(w, h)
        else:
            ar = "?"
        mm, ss = int(total_s) // 60, int(total_s) % 60
        return f"video, {ar}, {mm}:{ss:02d}, {duration_bucket(total_s)}", total_s
    except Exception:
        return "video, ?, ?, ?", 0.0


def detect_edit_pace(path: Path, ffmpeg_exe: str, duration_s: float) -> str:
    """Count scene changes via ffmpeg's select=gt(scene,...) filter; classify pace.
    fast = avg shot <1s, medium = 1-3s, slow = >3s."""
    if duration_s < 1.0:
        return "static"
    try:
        proc = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-nostats", "-i", str(path),
             "-filter:v", "select='gt(scene,0.3)',showinfo",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=60,
        )
        # showinfo prints one line per matched frame
        scenes = len(re.findall(r"\bn:\s*\d+\s+pts:", proc.stderr or ""))
        # +1 for the implicit first scene
        shots = scenes + 1
        avg_shot = duration_s / shots
        if avg_shot < 1.0:  return "fast"
        if avg_shot < 3.0:  return "medium"
        return "slow"
    except Exception:
        return "medium"


def extract_keyframes(path: Path, ffmpeg_exe: str, duration_s: float, n: int = 6, work_dir: Path = None) -> list[Path]:
    """Extract n evenly-spaced JPEG keyframes. Returns list of paths."""
    if work_dir is None:
        work_dir = path.parent / ".keyframes"
    work_dir.mkdir(exist_ok=True)
    stem = path.stem
    out_paths = []
    if duration_s < 0.5:
        # tiny clip — just one frame
        out = work_dir / f"{stem}_f1.jpg"
        subprocess.run(
            [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-frames:v", "1", "-q:v", "4", str(out)],
            capture_output=True, timeout=30,
        )
        if out.exists():
            out_paths.append(out)
        return out_paths
    timestamps = [duration_s * (i + 0.5) / n for i in range(n)]
    for i, ts in enumerate(timestamps):
        out = work_dir / f"{stem}_f{i}.jpg"
        if out.exists():
            out_paths.append(out)
            continue
        subprocess.run(
            [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{ts:.2f}", "-i", str(path),
             "-frames:v", "1", "-q:v", "4", "-vf", "scale='min(720,iw)':-2",
             str(out)],
            capture_output=True, timeout=30,
        )
        if out.exists():
            out_paths.append(out)
    return out_paths


# ---------- Gemini ----------

def init_gemini():
    if not GOOGLE_API_KEY:
        sys.exit("GOOGLE_API_KEY missing from ads_automation/.env")
    from google import genai
    return genai.Client(api_key=GOOGLE_API_KEY)


_TAXONOMY = """\
Use the EXACT enum values below. Pick the single best fit.

hook_category: one of [
  price-objection,           # "is it expensive?", "ne vale la pena?", cost concerns
  bill-savings,              # "risparmia in bolletta", lowering monthly bill
  state-incentives,          # PNRR, ecobonus, sconto in fattura, government programs
  geographic-qualifier,      # "abiti in un comune sotto i 50000 abitanti?", region-specific
  purchase-intent,           # "stai valutando un impianto?", consideration questions
  statistic,                 # numeric stat, percentage, hard data
  testimonial,               # customer story / review / experience
  urgency,                   # deadline, scadenza, limited time, "ultima chiamata"
  sustainability             # green / environment / planet framing
]
hook_text: the literal opening line in Italian, quoted verbatim (first ~3 seconds spoken or main image headline). If no text, write "(gancio visivo)".
pov: one of [customer, brand]
  - "customer" = first-person speaker IS a customer / customer-perspective framing
  - "brand"    = company spokesperson, brand voice, presenter, founder, employee
setting: one of [indoor, outdoor, graphic]
  - "graphic" for animations or graphic/text-only ads with no real environment
person_on_screen: one of [yes, no]
  - "yes" if any human is visible at any point; "no" otherwise (incl. graphic-only)
main_message: one of [price, incentives, savings, installation, sustainability]
"""

PROMPT_IMAGE = """\
You are analyzing an Italian advertising creative for a solar-energy / heat-pump company.
Examine the image and return a JSON object with the fields below.

""" + _TAXONOMY + """

Return ONLY the JSON object.
"""

PROMPT_VIDEO = """\
You are analyzing an Italian advertising VIDEO for a solar-energy / heat-pump company.
You are given {n_frames} keyframes sampled evenly across the {duration_label} clip,
plus the spoken transcript below.

Transcript:
\"\"\"
{transcript}
\"\"\"

Return a JSON object with the fields below.

""" + _TAXONOMY + """

Return ONLY the JSON object.
"""


def _bytes_part(path: Path):
    from google.genai import types
    with open(path, "rb") as f:
        data = f.read()
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "image/jpeg")
    return types.Part.from_bytes(data=data, mime_type=mime)


def analyze_image_with_gemini(client, path: Path) -> dict:
    from google.genai import types
    contents = [_bytes_part(path), PROMPT_IMAGE]
    schema = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in
            ["hook_category","hook_text","pov","setting","person_on_screen","main_message"]},
        "required": ["hook_category","hook_text","pov","setting","person_on_screen","main_message"],
    }
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        ),
    )
    return json.loads(resp.text)


def analyze_video_with_gemini(client, frame_paths: list[Path], duration_label: str, transcript: str) -> dict:
    from google.genai import types
    prompt = PROMPT_VIDEO.format(
        n_frames=len(frame_paths),
        duration_label=duration_label,
        transcript=(transcript or "(nessun parlato)")[:2000],
    )
    contents = [_bytes_part(p) for p in frame_paths] + [prompt]
    schema = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in
            ["hook_category","hook_text","pov","setting","person_on_screen","main_message"]},
        "required": ["hook_category","hook_text","pov","setting","person_on_screen","main_message"],
    }
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        ),
    )
    return json.loads(resp.text)


# ---------- Cache ----------

def load_cache() -> dict:
    cache = {}
    if CACHE_PATH.exists():
        with open(CACHE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                cache[row["Ad Name"]] = row
    return cache


def append_cache(row: dict):
    with open(CACHE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------- Sheet IO ----------

def get_gc():
    return gspread.service_account(filename=str(SA_PATH))


def read_transkripts(gc):
    ws = gc.open_by_key(SHEET_ID).worksheet(SOURCE_TAB)
    rows = ws.get_all_values()
    if not rows:
        return [], []
    header = rows[0]
    data = [r for r in rows[1:] if any(c.strip() for c in r)]
    out = []
    for r in data:
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        out.append(rec)
    return out, header


def push_to_sheet(gc, rows: list[dict]):
    ws = gc.open_by_key(SHEET_ID).worksheet(TARGET_TAB)
    table = [OUTPUT_COLS] + [[r.get(c, "") for c in OUTPUT_COLS] for r in rows]
    ws.clear()
    ws.update(values=table, range_name="A1", value_input_option="USER_ENTERED")
    print(f"Wrote {len(rows)} rows to '{TARGET_TAB}'")


# ---------- Main ----------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="Stop after N new analyses")
    p.add_argument("--push-only", action="store_true", help="Skip Gemini calls; just write cache to sheet")
    args = p.parse_args()

    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    print("Loading cache...")
    cache = load_cache()
    print(f"  {len(cache)} ads already analyzed")

    print("Reading Transkripts tab...")
    gc = get_gc()
    rows, _ = read_transkripts(gc)
    print(f"  {len(rows)} ads in Transkripts")

    if args.push_only:
        # Push cached rows in original Transkripts order
        ordered = [cache[r["Ad Name"]] for r in rows if r["Ad Name"] in cache]
        push_to_sheet(gc, ordered)
        return

    print("Indexing media files...")
    vids      = {p.stem.lower(): p for p in (MEDIA_ROOT/"videos").glob("*.mp4")}
    vids_orph = {p.stem.lower(): p for p in (MEDIA_ROOT/"videos"/"_orphans").glob("*.mp4")}
    imgs      = {}
    for ext in ("*.jpg","*.jpeg","*.png"):
        for p in (MEDIA_ROOT/"images").glob(ext):
            imgs[p.stem.lower()] = p
    print(f"  {len(vids)} videos, {len(vids_orph)} orphan videos, {len(imgs)} images")

    client = init_gemini()
    last_call = 0.0
    n_done = n_skip = n_nomedia = n_fail = 0

    for i, row in enumerate(rows, 1):
        ad_name = row.get("Ad Name", "").strip()
        if not ad_name:
            continue
        if ad_name in cache:
            n_skip += 1
            continue

        media_path, kind = find_media(ad_name, vids, vids_orph, imgs)
        if not media_path:
            n_nomedia += 1
            print(f"  [{i}/{len(rows)}] {ad_name[:60]}: NO MEDIA, skipping")
            continue

        # Compute Format & duration + Edit pace locally (free, exact, no tokens)
        if kind == "video":
            fmt, dur_s = probe_video(media_path, ffmpeg_exe)
            edit_pace = detect_edit_pace(media_path, ffmpeg_exe, dur_s)
            frames = extract_keyframes(media_path, ffmpeg_exe, dur_s, n=6)
            if not frames:
                n_fail += 1
                print(f"  [{i}/{len(rows)}] {ad_name[:60]}: keyframe extraction failed")
                continue
        else:
            fmt = probe_image(media_path)
            edit_pace = "static"
            frames = []
            dur_s = 0

        # Throttle to ~15 RPM
        wait = MIN_INTERVAL_SEC - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)

        try:
            if kind == "video":
                res = analyze_video_with_gemini(client, frames, fmt.split(", ",1)[1], row.get("Transcript",""))
            else:
                res = analyze_image_with_gemini(client, media_path)
            last_call = time.time()
        except Exception as e:
            err = str(e)
            print(f"  [{i}/{len(rows)}] {ad_name[:60]}: FAILED -- {err[:200]}")
            n_fail += 1
            if "429" in err or "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err:
                print("    rate-limited, sleeping 60s")
                time.sleep(60)
            continue

        full = {
            "Ad Name":     ad_name,
            "Product Type": row.get("Product Type", ""),
            "Channel":      row.get("Channel", ""),
            "Media Type":   row.get("Media Type", ""),
            "Promo":        row.get("Promo", ""),
            "Description":  row.get("Description", ""),
            "Transcript":   row.get("Transcript", ""),
            "Hook category":    res.get("hook_category", ""),
            "Hook text":        res.get("hook_text", ""),
            "POV":              res.get("pov", ""),
            "Setting":          res.get("setting", ""),
            "Edit pace":        edit_pace,
            "Format & duration": fmt,
            "Person on screen": res.get("person_on_screen", ""),
            "Main message":     res.get("main_message", ""),
        }
        append_cache(full)
        cache[ad_name] = full
        n_done += 1
        print(f"  [{i}/{len(rows)}] {ad_name[:60]}  -- {kind}, {fmt}")

        # Periodic push to sheet every 20
        if n_done % 20 == 0:
            ordered = [cache[r["Ad Name"]] for r in rows if r["Ad Name"] in cache]
            try:
                push_to_sheet(gc, ordered)
            except Exception as e:
                print(f"  sheet push failed (will retry later): {e}")

        if args.limit and n_done >= args.limit:
            break

    # Final push
    ordered = [cache[r["Ad Name"]] for r in rows if r["Ad Name"] in cache]
    push_to_sheet(gc, ordered)
    print(f"\nDone. analyzed={n_done}, cached-skip={n_skip}, no-media={n_nomedia}, failed={n_fail}")


if __name__ == "__main__":
    main()
