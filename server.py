"""Creator Studio: YouTube clip downloader + ClipForge auto-editor in one localhost app.

Run:  python server.py   then open http://127.0.0.1:7860
"""
import os
import subprocess
import sys

RESTART_CODE = 3   # the server exits with this to ask for a fresh start (after an update)


def _supervise() -> int:
    """Run the real server as a child process, and start it again whenever it asks to restart.
    That lets an update apply itself without closing the app."""
    env = {**os.environ, "CS_CHILD": "1"}
    # keep the console when started from a terminal (to see errors); otherwise never flash a window
    flags = 0x08000000 if os.name == "nt" and not (sys.stdout and sys.stdout.isatty()) else 0
    while True:
        try:
            code = subprocess.call([sys.executable, os.path.abspath(__file__)], env=env, creationflags=flags)
        except KeyboardInterrupt:
            return 0
        if code != RESTART_CODE:
            return code
        env["NO_BROWSER"] = "1"   # don't open another browser tab after a restart


if __name__ == "__main__" and os.environ.get("CS_CHILD") != "1":
    sys.exit(_supervise())

import json        # noqa: E402  (the supervisor above stays light: no app imports)
import threading   # noqa: E402
import time        # noqa: E402
import webbrowser  # noqa: E402

from flask import g, jsonify, request, send_from_directory  # noqa: E402

import updater  # noqa: E402

from clipper import app          # noqa: E402  the downloader's Flask app (its /api/* routes)
from forge_routes import bp, render_lock  # noqa: E402  ClipForge + shared library routes
from ffmpeg_tools import ensure_ffmpeg_in_background  # noqa: E402

app.register_blueprint(bp)
ensure_ffmpeg_in_background()    # PCs without ffmpeg get a private copy on first start
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ------------------------------------------- auto-update + version badge (next to Discord)
HERE = os.path.dirname(os.path.abspath(__file__))
SUPERVISED = os.environ.get("CS_CHILD") == "1"   # True when we can restart ourselves
RUNNING_SHA = updater.installed_sha()             # the code this server process actually loaded
CHECK_EVERY = 300                                 # seconds between GitHub checks
_latest = {"sha": None, "at": 0.0}
_updating = threading.Event()
_inflight = {"n": 0}
_inflight_lock = threading.Lock()
QUIET = ("/api/version", "/api/update", "/api/restart")   # polling calls don't count as "busy"


def app_version() -> str:
    try:
        with open(os.path.join(HERE, "package.json"), encoding="utf-8") as f:
            return json.load(f).get("version") or ""
    except Exception:
        return ""


def latest_sha(force=False):
    if force or time.time() - _latest["at"] > CHECK_EVERY:   # ask GitHub at most every 5 minutes
        _latest.update(sha=updater.latest_sha(), at=time.time())
    return _latest["sha"]


@app.before_request
def _count_request():
    if request.path.startswith("/api/") and request.path not in QUIET:
        with _inflight_lock:
            _inflight["n"] += 1
        g.counted = True


@app.teardown_request
def _uncount_request(_exc):
    if g.pop("counted", False):
        with _inflight_lock:
            _inflight["n"] -= 1


def busy() -> bool:
    """True while a download, render or other request is running, so a restart would cut it off."""
    return _inflight["n"] > 0 or render_lock.locked() or _updating.is_set()


def start_update() -> None:
    if _updating.is_set():
        return
    _updating.set()

    def work():
        try:
            updater.update()
            latest_sha(force=True)
        except Exception as e:
            print(f"[auto-update] failed: {e}", flush=True)
        finally:
            _updating.clear()
    threading.Thread(target=work, daemon=True).start()


def needs_update(latest) -> bool:
    have = updater.installed_sha()
    return bool(latest and have and latest != have) or not updater.packages_ready()


def _auto_update_loop():
    """Check GitHub every few minutes and download any update straight away."""
    time.sleep(15)
    while True:
        try:
            if needs_update(latest_sha(force=True)):
                start_update()
        except Exception as e:
            print(f"[auto-update] check failed: {e}", flush=True)
        time.sleep(CHECK_EVERY)


@app.get("/api/version")
def version_status():
    have = updater.installed_sha()
    latest = latest_sha(force=request.args.get("refresh") == "1")
    if _updating.is_set():
        state, msg = "updating", "Downloading an update…"
    elif have and RUNNING_SHA and have != RUNNING_SHA:
        state = "restart"
        msg = ("Update ready. It installs by itself as soon as you're not using the app (takes a few seconds). "
               "Click to install now." if SUPERVISED else
               "Update downloaded. Close and reopen Creator Studio to finish.")
    elif needs_update(latest):
        state, msg = "outdated", "Out of date. Updating automatically…"
    elif not latest:
        state, msg = "unknown", "Couldn't check for updates (offline?)."
    else:
        state, msg = "current", "Up to date."
    return jsonify(version=app_version(), sha=(RUNNING_SHA or have or "")[:7], latest=(latest or "")[:7],
                   state=state, message=msg, can_restart=SUPERVISED, busy=busy())


@app.post("/api/update")
def run_update():
    start_update()
    return jsonify(ok=True)


@app.post("/api/restart")
def restart():
    """Apply a downloaded update: the supervisor starts a fresh server on the new code."""
    if not SUPERVISED:
        return jsonify(error="Close and reopen Creator Studio to finish the update."), 400
    if busy():
        return jsonify(error="Busy: waiting for downloads and renders to finish."), 409
    threading.Timer(0.5, lambda: os._exit(RESTART_CODE)).start()
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
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 7860))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
