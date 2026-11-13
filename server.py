"""Creator Studio: YouTube clip downloader + ClipForge auto-editor in one localhost app.

Run:  python server.py   then open http://127.0.0.1:7860
"""
import os
import threading
import webbrowser

from flask import send_from_directory

from clipper import app          # the downloader's Flask app (its /api/* routes)
from forge_routes import bp      # ClipForge + shared library routes

app.register_blueprint(bp)
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
def shell():
    return send_from_directory(STATIC, "index.html")


@app.get("/forge")
def forge_page():
    return send_from_directory(STATIC, "forge.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
