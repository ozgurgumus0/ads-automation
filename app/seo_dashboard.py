"""
SEO dashboard: the SEO & GEO section of ads_automation, kept apart from the ads sync.

Fills the "SEO-dashboard" Google Sheet (SEO_SHEETS_ID) that the SEO & GEO
dashboard reads: GA4, Search Console, Snowflake leads, Peec and CRM organic leads, see
fetchers/seo.py and seo_sync.py. Nothing here touches main_metrics or the
other sources, and main.py doesn't know about it.

Command line (what Task Scheduler runs every morning):
    python seo_dashboard.py                    incremental refresh of every source
    python seo_dashboard.py --only gsc,peec    just these sources
    python seo_dashboard.py --full             refetch the whole history

Web: app.py registers `bp`, served at /seo.
"""

import argparse
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv
from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH)

SOURCES = {  # sub-source -> label (keys as in the sheet's _status tab)
    "ga4_sections": "GA4 sections",
    "ga4_product_choice": "GA4 product choice",
    "ga4_product_leads": "GA4 leads by product",
    "ga4_channels": "GA4 channel mix",
    "ga4_monthly_sessions": "GA4 monthly sessions (report)",
    "gsc": "Search Console",
    "snowflake_leads": "Snowflake leads",
    "crm_organic": "CRM organic",
    "peec": "Peec AI",
}
TASK_NAME = "ads_automation - SEO daily"
# Apps Script deployment id of the (separate) SEO & GEO front end, optional: only used for a link.
# Only these keys can be read/written from the SEO page. Secrets are never
# sent back to the browser: the page only learns whether they are set.
CONFIG_KEYS = {"SEO_SHEETS_ID": False, "PEEC_PAT": True,   # key -> is_secret
               "SNOWFLAKE_ACCOUNT": False, "SNOWFLAKE_USER": False, "SNOWFLAKE_ROLE": False,
               "SNOWFLAKE_WAREHOUSE": False, "SNOWFLAKE_DATABASE": False}

LOCK_PATH = os.path.join(BASE_DIR, "logs", "seo_dashboard.lock")
LOCK_STALE_S = 3 * 3600   # a crashed run's lock stops counting after 3 h

bp = Blueprint("seo_dashboard", __name__, url_prefix="/seo")


def _pid_alive(pid):
    """True if the process exists. (Not os.kill(pid, 0): on Windows that terminates it.)"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259            # STILL_ACTIVE


def _running():
    """A lock counts only while the run that took it is alive (a killed run, e.g. its
    window closed, can't remove its lock) and for at most LOCK_STALE_S."""
    if not os.path.exists(LOCK_PATH) or time.time() - os.path.getmtime(LOCK_PATH) >= LOCK_STALE_S:
        return False
    try:
        pid = int(open(LOCK_PATH).read().split()[0])
    except (OSError, ValueError, IndexError):
        return True
    return _pid_alive(pid)


def _acquire_lock():
    """One refresh at a time, whether started by the page or the morning task."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if _running():
        return False
    with open(LOCK_PATH, "w") as f:
        f.write(f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M')}")
    return True


def _env():
    out = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, _, v = s.partition("=")
                    out[k.strip()] = v.strip()
    return out


def _write_env(updates):
    lines = open(ENV_PATH, encoding="utf-8").readlines() if os.path.exists(ENV_PATH) else []
    done, out = set(), []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if k in updates:
            out.append(f"{k}={updates[k]}\n")
            done.add(k)
        else:
            out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out += [f"{k}={v}\n" for k, v in updates.items() if k not in done]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(out)


def _schedule():
    """Next/last run of the morning task (empty if the task doesn't exist)."""
    ps = (f"$i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}' -ErrorAction Stop; "
          "[pscustomobject]@{next=$i.NextRunTime.ToString('yyyy-MM-dd HH:mm');"
          "last=$i.LastRunTime.ToString('yyyy-MM-dd HH:mm');result=$i.LastTaskResult} | ConvertTo-Json")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return {}


@bp.route("/")
def page():
    return render_template("seo_dashboard.html", sources=SOURCES)


@bp.route("/status")
def status():
    env = _env()
    sheet_id = env.get("SEO_SHEETS_ID", "")
    rows, error = [], ""
    if sheet_id:
        try:
            import seo_sync
            rows = seo_sync._read(seo_sync._open(sheet_id), "_status")
        except Exception as e:
            error = str(e)[:300]
    dep = env.get("SEO_DASHBOARD_DEPLOYMENT_ID", "")
    return jsonify({
        "sources": [{**{"source": k, "label": v}, **next((r for r in rows if r.get("source") == k), {})}
                    for k, v in SOURCES.items()],
        "error": error,
        "running": _running(),
        "schedule": _schedule(),
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else "",
        "dashboard_url": f"https://script.google.com/macros/s/{dep}/exec" if dep else "",
    })


@bp.route("/config", methods=["GET"])
def get_config():
    env = _env()
    return jsonify({k: ("set" if env.get(k) else "") if secret else env.get(k, "")
                    for k, secret in CONFIG_KEYS.items()})


@bp.route("/config", methods=["POST"])
def save_config():
    data = request.get_json(force=True)
    safe = {k: str(v).strip() for k, v in data.items() if k in CONFIG_KEYS and str(v).strip()}
    if not safe:
        return jsonify({"ok": False, "error": "Nothing to save"}), 400
    _write_env(safe)
    return jsonify({"ok": True, "saved": sorted(safe)})


@bp.route("/snowflake/login", methods=["POST"])
def snowflake_login():
    """Snowflake SSO: opens the browser if the cached token expired, so the morning
    run (which can't click anything) finds a fresh token."""
    from fetchers import seo
    script = os.path.join(BASE_DIR, "fetchers", "seo_snowflake.py")
    try:
        r = subprocess.run(seo.SNOWFLAKE_PYTHON.split() + [script, "--check"], cwd=BASE_DIR, capture_output=True,
                           text=True, timeout=180, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "No login within 3 minutes."})
    if r.returncode != 0:
        return jsonify({"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]})
    return jsonify({"ok": True, **json.loads(r.stdout.strip().splitlines()[-1])})


@bp.route("/run", methods=["POST"])
def run():
    data = request.get_json(force=True)
    only = [s for s in data.get("only", []) if s in SOURCES]
    cmd = [sys.executable, "-u", os.path.join(BASE_DIR, "seo_dashboard.py")]
    if only and len(only) < len(SOURCES):
        cmd += ["--only", ",".join(only)]
    if data.get("full"):
        cmd.append("--full")

    def generate():   # the lock lives in main(), so the morning task is covered too
        yield f"data: $ python seo_dashboard.py {' '.join(cmd[3:])}\n\n"
        proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, encoding="utf-8", errors="replace")
        for line in iter(proc.stdout.readline, ""):
            if line.strip():
                yield f"data: {line.rstrip()}\n\n"
        proc.wait()
        yield f"data: (exit code {proc.returncode})\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="SEO: refresh the SEO & GEO sheet.")
    p.add_argument("--only", default="", help="comma-separated sources: " + ", ".join(SOURCES))
    p.add_argument("--full", action="store_true", help="refetch the whole history (slow: ~30 min)")
    args = p.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    if only and only - set(SOURCES):
        sys.exit(f"Unknown source(s): {sorted(only - set(SOURCES))}. Allowed: {', '.join(SOURCES)}")
    sheet_id = os.getenv("SEO_SHEETS_ID")
    if not sheet_id:
        sys.exit("SEO_SHEETS_ID is not set in .env (SEO lead page > Settings).")
    if not _acquire_lock():
        sys.exit("A refresh is already running (from the SEO page or the morning task). Try again later.")
    try:
        import seo_sync
        seo_sync.sync(sheet_id, only=only, full=args.full)
    finally:
        os.remove(LOCK_PATH)


if __name__ == "__main__":
    main()
