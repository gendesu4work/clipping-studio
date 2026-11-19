"""Creator Studio: YouTube clip downloader + ClipForge auto-editor in one localhost app.

Run:  python server.py   then open http://127.0.0.1:7860
"""
import json
import os
import threading
import time
import webbrowser

from flask import jsonify, request, send_from_directory

import updater

from clipper import app          # the downloader's Flask app (its /api/* routes)
from forge_routes import bp      # ClipForge + shared library routes
from ffmpeg_tools import ensure_ffmpeg_in_background

app.register_blueprint(bp)
ensure_ffmpeg_in_background()    # PCs without ffmpeg get a private copy on first start
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ------------------------------------------------ version badge (next to Discord)
HERE = os.path.dirname(os.path.abspath(__file__))
RUNNING_SHA = updater.installed_sha()   # the code this server process actually loaded
_latest = {"sha": None, "at": 0.0}
_updating = threading.Event()


def app_version() -> str:
    try:
        with open(os.path.join(HERE, "package.json"), encoding="utf-8") as f:
            return json.load(f).get("version") or ""
    except Exception:
        return ""


def latest_sha(force=False):
    if force or time.time() - _latest["at"] > 300:   # ask GitHub at most every 5 minutes
        _latest.update(sha=updater.latest_sha(), at=time.time())
    return _latest["sha"]


@app.get("/api/version")
def version_status():
    have = updater.installed_sha()
    latest = latest_sha(force=request.args.get("refresh") == "1")
    if _updating.is_set():
        state, msg = "updating", "Updating… keep the app open."
    elif have and RUNNING_SHA and have != RUNNING_SHA:
        state, msg = "restart", "Update downloaded. Close and reopen Creator Studio to finish."
    elif not updater.packages_ready():
        state, msg = "outdated", "Some packages didn't finish installing. Click to retry."
    elif not latest:
        state, msg = "unknown", "Couldn't check for updates (offline?)."
    elif have == latest:
        state, msg = "current", "Up to date."
    else:
        state, msg = "outdated", "Update available. Click to update."
    return jsonify(version=app_version(), sha=(RUNNING_SHA or have or "")[:7],
                   latest=(latest or "")[:7], state=state, message=msg)


@app.post("/api/update")
def run_update():
    if not _updating.is_set():
        _updating.set()

        def work():
            try:
                updater.update()
                latest_sha(force=True)
            finally:
                _updating.clear()
        threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


@app.get("/")
def shell():
    return send_from_directory(STATIC, "index.html")


@app.get("/twitch")
def twitch_page():
    return send_from_directory(STATIC, "twitch.html")


@app.get("/shorts")
def shorts_page():
    return send_from_directory(STATIC, "shorts.html")


@app.get("/forge")
def forge_page():
    return send_from_directory(STATIC, "forge.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
