import os
import subprocess
import sys
import json
from flask import Flask, render_template, request, Response, stream_with_context, jsonify

from archive_tool import get_archive_config, run_archive

app = Flask(__name__)

# SEO (SEO & GEO) lives in its own module and page (/seo), separate from the ads sync.
from seo_dashboard import bp as seo_bp
app.register_blueprint(seo_bp)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE_DIR, ".env")

# Keys exposed in the UI credentials panel
CREDENTIAL_KEYS = [
    "META_ACCESS_TOKEN",
    "OUTBRAIN_API_KEY",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "TIKTOK_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "OUTBRAIN_ACCOUNT_ID",
    "TIKTOK_ADVERTISER_ID",
    "GOOGLE_SHEETS_ID",
    "WHATSAPP_SHEETS_ID",
]


def _read_env():
    result = {}
    if not os.path.exists(ENV_PATH):
        return result
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, v = stripped.partition("=")
                result[k.strip()] = v.strip()
    return result


def _write_env(updates: dict):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.readlines()

    updated = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                updated.add(k)
                continue
        new_lines.append(line)

    for k, v in updates.items():
        if k not in updated:
            new_lines.append(f"{k}={v}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


@app.route("/config", methods=["GET"])
def get_config():
    env = _read_env()
    return jsonify({k: env.get(k, "") for k in CREDENTIAL_KEYS})


@app.route("/config", methods=["POST"])
def save_config():
    data = request.get_json(force=True)
    safe = {k: v for k, v in data.items() if k in CREDENTIAL_KEYS}
    if not safe:
        return jsonify({"ok": False, "error": "No valid keys provided"}), 400
    _write_env(safe)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(force=True)
    start_date = data.get("start_date", "")
    end_date = data.get("end_date", "")
    source = data.get("source", "ALL")
    target = data.get("target", "test")
    if target not in ("test", "production"):
        target = "test"

    cmd = [
        sys.executable, "-u", "main.py",
        "--start", start_date,
        "--end", end_date,
        "--source", source,
        "--target", target,
    ]

    def generate():
        process = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

        for line in iter(process.stdout.readline, ""):
            line = line.rstrip("\n\r")
            if line:
                yield f"data: {line}\n\n"

        process.stdout.close()
        process.wait()
        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/archive/status", methods=["GET"])
def archive_status():
    cfg = get_archive_config()
    if not cfg:
        return jsonify({"configured": False})
    return jsonify({"configured": True, **cfg})


@app.route("/archive/run", methods=["POST"])
def archive_run():
    data = request.get_json(force=True)
    new_cutoff = data.get("new_cutoff", "")

    def generate():
        for line in run_archive(new_cutoff):
            yield f"data: {line}\n\n"
        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/creative-feed/status", methods=["GET"])
def creative_feed_status():
    import creative_feed
    st = {}
    if os.path.exists(creative_feed.STATUS_PATH):
        with open(creative_feed.STATUS_PATH, encoding="utf-8") as f:
            st = json.load(f)
    return jsonify({"package_ready": creative_feed.package_ready(), "next_source": creative_feed.source_name(), **st})


@app.route("/creative-feed/run", methods=["POST"])
def creative_feed_run():
    """Snowflake cohort feed -> New_Data + Lead_Contract_Cohort (see creative_feed.py)."""
    data = request.get_json(force=True)
    cmd = [sys.executable, "-u", "creative_feed.py"] + (["--dry-run"] if data.get("dry_run") else [])

    def generate():
        process = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, encoding="utf-8", errors="replace")
        for line in iter(process.stdout.readline, ""):
            line = line.rstrip("\n\r")
            if line:
                yield f"data: {line}\n\n"
        process.stdout.close()
        process.wait()
        yield "data: __DONE__\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
