"""
Download every ad image and ad video from a Meta (Facebook) ad account.

Usage:
    python download_meta_creatives.py                       # uses META_AD_ACCOUNT_ID from .env
    python download_meta_creatives.py __META_ACCOUNT_ID__      # override account id
    python download_meta_creatives.py --images-only
    python download_meta_creatives.py --videos-only
    python download_meta_creatives.py --out C:/path/to/dir

Reads from ads_automation/.env:
    META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, META_API_VERSION
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / "ads_automation" / ".env")

GRAPH = "https://graph.facebook.com"
PAGE_LIMIT = 100  # max per page
SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe(name: str, max_len: int = 80) -> str:
    s = SAFE.sub("_", (name or "").strip()).strip("._")
    return (s[:max_len] or "untitled")


def graph_get(path: str, params: dict, version: str) -> dict:
    url = f"{GRAPH}/{version}/{path}"
    for attempt in range(1, 6):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) or '"code":4' in r.text:
            wait = 30 * attempt
            print(f"    transient {r.status_code}, retry in {wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"GET {url} -> {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"giving up on {url}")


def _get_with_retry(url: str, params: dict | None = None) -> dict:
    """GET with retry on transient errors. Used for both first-page and paging.next URLs."""
    last_err = None
    for attempt in range(1, 7):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 30 * attempt
                print(f"    transient {r.status_code}, retry in {wait}s")
                time.sleep(wait)
                continue
            # 400 mid-pagination has been observed transiently from Meta on deep cursors
            if r.status_code == 400 and attempt <= 3:
                wait = 20 * attempt
                print(f"    400 (retry {attempt}/3) in {wait}s — {r.text[:160]}")
                time.sleep(wait)
                continue
            raise RuntimeError(f"GET {url[:120]}... -> {r.status_code} {r.text[:300]}")
        except requests.RequestException as e:
            last_err = e
            wait = 15 * attempt
            print(f"    network error, retry in {wait}s: {e}")
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url[:120]}: {last_err}")


def paginate(path: str, fields: list[str], token: str, version: str):
    url = f"{GRAPH}/{version}/{path}"
    params = {"access_token": token, "fields": ",".join(fields), "limit": PAGE_LIMIT}
    next_url = None
    while True:
        if next_url:
            data = _get_with_retry(next_url)
        else:
            data = _get_with_retry(url, params)
        for item in data.get("data", []):
            yield item
        next_url = (data.get("paging") or {}).get("next")
        if not next_url:
            return


def stream_download(url: str, dest: Path) -> int:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    tmp.rename(dest)
    return dest.stat().st_size


def ext_from_url(url: str, fallback: str) -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mov", "webm"}:
        return ext
    return fallback


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _walk_creative_assets(creative: dict):
    """Extract every image_hash and video_id referenced by an ad creative."""
    images, videos = set(), set()
    if not creative:
        return images, videos
    if creative.get("image_hash"):
        images.add(creative["image_hash"])
    if creative.get("video_id"):
        videos.add(str(creative["video_id"]))
    if creative.get("thumbnail_url"):
        pass  # not addressable by hash; skip
    oss = creative.get("object_story_spec") or {}
    link_data = oss.get("link_data") or {}
    if link_data.get("image_hash"):
        images.add(link_data["image_hash"])
    for child in (link_data.get("child_attachments") or []):
        if child.get("image_hash"):
            images.add(child["image_hash"])
        if child.get("video_id"):
            videos.add(str(child["video_id"]))
    video_data = oss.get("video_data") or {}
    if video_data.get("video_id"):
        videos.add(str(video_data["video_id"]))
    if video_data.get("image_hash"):
        images.add(video_data["image_hash"])
    photo_data = oss.get("photo_data") or {}
    if photo_data.get("image_hash"):
        images.add(photo_data["image_hash"])
    afs = creative.get("asset_feed_spec") or {}
    for img in (afs.get("images") or []):
        if img.get("hash"):
            images.add(img["hash"])
    for vid in (afs.get("videos") or []):
        if vid.get("video_id"):
            videos.add(str(vid["video_id"]))
        if vid.get("thumbnail_hash"):
            images.add(vid["thumbnail_hash"])
    return images, videos


def build_ad_asset_maps(act: str, token: str, version: str):
    """Build {image_hash: [ad_name,...]} and {video_id: [ad_name,...]} by walking every ad."""
    fields = [
        "name",
        "creative{id,image_hash,video_id,object_story_spec,asset_feed_spec}",
    ]
    img_to_ads, vid_to_ads = {}, {}
    n_ads = 0
    print(f"\n=== ADS ===  /{act}/ads (building name map)")
    for ad in paginate(f"{act}/ads", fields, token, version):
        n_ads += 1
        ad_name = ad.get("name") or ""
        creative = ad.get("creative") or {}
        imgs, vids = _walk_creative_assets(creative)
        for h in imgs:
            img_to_ads.setdefault(h, []).append(ad_name)
        for v in vids:
            vid_to_ads.setdefault(v, []).append(ad_name)
    print(f"  {n_ads} ads scanned -> {len(img_to_ads)} image hashes, {len(vid_to_ads)} video ids mapped to ad names")
    return img_to_ads, vid_to_ads


def _name_from_ads(ad_names: list[str]) -> str:
    """Pick a representative ad name from possibly many; trim sensibly."""
    if not ad_names:
        return ""
    # Prefer the first non-empty, longest informative-looking name
    seen = []
    for n in ad_names:
        if n and n not in seen:
            seen.append(n)
        if len(seen) >= 1:
            break
    return seen[0] if seen else ""


def _next_dest(out_dir: Path, base: str, ext: str) -> Path:
    """Return out_dir/<base>.<ext> if free, else <base>_2.<ext>, _3.<ext>, ..."""
    primary = out_dir / f"{base}.{ext}"
    if not primary.exists():
        return primary
    i = 2
    while True:
        cand = out_dir / f"{base}_{i}.{ext}"
        if not cand.exists():
            return cand
        i += 1


def download_images(act: str, token: str, version: str, out_dir: Path,
                    img_to_ads: dict, include_orphans: bool = False):
    out_dir.mkdir(parents=True, exist_ok=True)
    orphan_dir = out_dir / "_orphans"
    fields = ["hash", "name", "url", "permalink_url", "width", "height"]
    print(f"\n=== IMAGES ===  /{act}/adimages")
    n_ok = n_fail = n_orphan_dl = n_orphan_skip = 0
    total_bytes = 0
    for img in paginate(f"{act}/adimages", fields, token, version):
        url = img.get("url")
        if not url:
            continue
        h = img.get("hash") or "nohash"
        ad_name = _name_from_ads(img_to_ads.get(h, []))
        if ad_name:
            base = safe(ad_name)
            target_dir = out_dir
        else:
            if not include_orphans:
                n_orphan_skip += 1
                continue
            n_orphan_dl += 1
            base = safe(img.get("name") or h)
            orphan_dir.mkdir(parents=True, exist_ok=True)
            target_dir = orphan_dir
        ext = ext_from_url(url, "jpg")
        dest = _next_dest(target_dir, base, ext)
        try:
            size = stream_download(url, dest)
            total_bytes += size
            n_ok += 1
            print(f"  [{n_ok}] {dest.relative_to(out_dir)}  ({fmt_size(size)})")
        except Exception as e:
            n_fail += 1
            print(f"  FAIL {dest.name}: {e}")
    print(f"images: {n_ok} downloaded ({n_orphan_dl} orphans), {n_fail} failed, {n_orphan_skip} orphan (skipped), {fmt_size(total_bytes)}")


def download_videos(act: str, token: str, version: str, out_dir: Path,
                    vid_to_ads: dict, include_orphans: bool = False):
    out_dir.mkdir(parents=True, exist_ok=True)
    orphan_dir = out_dir / "_orphans"
    # Sidecar: which video_ids we've already pulled, keyed by id -> filename.
    ledger_path = out_dir / "_downloaded.json"
    ledger = {}
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception:
            ledger = {}
    fields = ["id", "title", "source", "permalink_url", "created_time"]
    print(f"\n=== VIDEOS ===  /{act}/advideos  (already have {len(ledger)})")
    n_ok = n_fail = n_nosrc = n_orphan_dl = n_orphan_skip = n_skip = 0
    total_bytes = 0
    for vid in paginate(f"{act}/advideos", fields, token, version):
        vid_id = str(vid.get("id") or "")
        if vid_id in ledger and (out_dir / ledger[vid_id]).exists():
            n_skip += 1
            continue
        title = vid.get("title") or vid_id or "video"
        source = vid.get("source")
        if not source:
            try:
                detail = graph_get(vid_id, {"access_token": token, "fields": "source"}, version)
                source = detail.get("source")
            except Exception:
                source = None
        if not source:
            n_nosrc += 1
            print(f"  no source for {vid_id} ({title[:60]})")
            continue
        ad_name = _name_from_ads(vid_to_ads.get(vid_id, []))
        if ad_name:
            base = safe(ad_name)
            target_dir = out_dir
        else:
            if not include_orphans:
                n_orphan_skip += 1
                continue
            n_orphan_dl += 1
            base = safe(title)
            orphan_dir.mkdir(parents=True, exist_ok=True)
            target_dir = orphan_dir
        ext = ext_from_url(source, "mp4")
        dest = _next_dest(target_dir, base, ext)
        try:
            size = stream_download(source, dest)
            total_bytes += size
            n_ok += 1
            ledger[vid_id] = str(dest.relative_to(out_dir).as_posix())
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            print(f"  [{n_ok}] {dest.relative_to(out_dir)}  ({fmt_size(size)})")
        except Exception as e:
            n_fail += 1
            print(f"  FAIL {dest.name}: {e}")
    print(f"videos: {n_ok} downloaded ({n_orphan_dl} orphans), {n_skip} skipped (already had), {n_fail} failed, {n_nosrc} without source, {n_orphan_skip} orphan (skipped), {fmt_size(total_bytes)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("account_id", nargs="?", help="Ad account id (numeric, no 'act_' prefix). Defaults to META_AD_ACCOUNT_ID env.")
    p.add_argument("--images-only", action="store_true")
    p.add_argument("--videos-only", action="store_true")
    p.add_argument("--include-orphans", action="store_true",
                   help="Also download assets not linked to any ad, into a _orphans/ subfolder")
    p.add_argument("--refresh-map", action="store_true",
                   help="Force re-scan of /ads even if a cached _asset_maps.json exists")
    p.add_argument("--out", default=str(HERE / "meta_creatives"), help="Output root directory")
    args = p.parse_args()

    token = os.getenv("META_ACCESS_TOKEN")
    version = os.getenv("META_API_VERSION", "v21.0")
    account_id = args.account_id or os.getenv("META_AD_ACCOUNT_ID", "").lstrip("act_")
    if not token:
        sys.exit("META_ACCESS_TOKEN missing in ads_automation/.env")
    if not account_id:
        sys.exit("Provide an ad account id (CLI arg or META_AD_ACCOUNT_ID env)")

    act = f"act_{account_id}"
    out_root = Path(args.out) / account_id
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"Downloading creatives from {act}")
    print(f"Output: {out_root}")

    cache_path = out_root / "_asset_maps.json"
    img_to_ads = vid_to_ads = None
    if cache_path.exists() and not args.refresh_map:
        try:
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
            img_to_ads = blob["img_to_ads"]
            vid_to_ads = blob["vid_to_ads"]
            print(f"Loaded cached ad-asset map from {cache_path.name} "
                  f"({len(img_to_ads)} images, {len(vid_to_ads)} videos mapped)")
        except Exception as e:
            print(f"  cache unreadable, rebuilding: {e}")
            img_to_ads = vid_to_ads = None
    if img_to_ads is None:
        img_to_ads, vid_to_ads = build_ad_asset_maps(act, token, version)
        cache_path.write_text(
            json.dumps({"img_to_ads": img_to_ads, "vid_to_ads": vid_to_ads}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Cached ad-asset map to {cache_path.name}")

    if not args.videos_only:
        download_images(act, token, version, out_root / "images", img_to_ads, args.include_orphans)
    if not args.images_only:
        download_videos(act, token, version, out_root / "videos", vid_to_ads, args.include_orphans)


if __name__ == "__main__":
    main()
