"""License keys: Creator Studio only runs on a PC that has a valid key made for that PC.

How it works
- Every PC has a machine code (from Windows' machine ID, hashed). The buyer sends it to the seller.
- The seller signs (machine code + customer number + optional expiry) with a private key that never
  leaves the seller's PC (see tools/make_license.py). That signature is the license key.
- This file only holds the PUBLIC key: it can check a key but can't make one, so it's safe to ship.
- A key made for one PC is rejected on any other PC, so it can't be shared or resold.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import struct

from flask import jsonify, redirect, render_template_string, request

PUBLIC_KEY = "owuH7QCOfSLQZ+h5xn2IJRa9AOTJsb4vihJ95DO9hbU="   # filled in by: python tools/make_license.py init
HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "license.key")
EPOCH = dt.date(2024, 1, 1)
VERSION = 1
PUBLIC_PATHS = {"/activate", "/api/license", "/static/icon.png", "/favicon.ico"}


# ---------------------------------------------------------------- machine code
def _machine_id() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        import uuid
        return f"node-{uuid.getnode():x}"


def machine_hash(raw: str | None = None) -> bytes:
    return hashlib.sha256(b"creator-studio|" + (raw or _machine_id()).encode()).digest()[:10]


def machine_code(raw: str | None = None) -> str:
    """What the buyer sends you, e.g. 7KQ2-M9XA-..."""
    s = base64.b32encode(machine_hash(raw)).decode().rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def parse_machine_code(code: str) -> bytes:
    s = "".join(ch for ch in code.upper() if ch.isalnum())
    raw = base64.b32decode(s + "=" * (-len(s) % 8))
    if len(raw) != 10:
        raise ValueError("That machine code isn't the right length.")
    return raw


# ---------------------------------------------------------------- key format
# payload = version (1) | machine hash (10) | customer number (4) | expiry, days since 2024-01-01, 0 = never (2)
def pack_payload(machine: bytes, customer: int, expires: dt.date | None) -> bytes:
    days = 0 if expires is None else max(1, (expires - EPOCH).days)
    return struct.pack(">B10sIH", VERSION, machine, customer, days)


def format_key(payload: bytes, signature: bytes) -> str:
    s = base64.b32encode(payload + signature).decode().rstrip("=")
    return "-".join(s[i:i + 5] for i in range(0, len(s), 5))


class LicenseError(Exception):
    pass


def check_key(key: str, raw_machine: str | None = None) -> dict:
    """Returns the license details, or raises LicenseError with a message the buyer can understand."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    s = "".join(ch for ch in (key or "").upper() if ch.isalnum())
    try:
        blob = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise LicenseError("That isn't a valid license key. Check that you pasted all of it.")
    if len(blob) != 17 + 64:
        raise LicenseError("That isn't a valid license key. Check that you pasted all of it.")
    payload, sig = blob[:17], blob[17:]
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY)).verify(sig, payload)
    except (InvalidSignature, ValueError):
        raise LicenseError("That isn't a valid license key.")
    version, machine, customer, days = struct.unpack(">B10sIH", payload)
    if version != VERSION:
        raise LicenseError("This license key is for a different version of Creator Studio.")
    if machine != machine_hash(raw_machine):
        raise LicenseError("This license key was made for a different computer. "
                           "Send the seller the machine code shown here to get a key for this PC.")
    expires = EPOCH + dt.timedelta(days=days) if days else None
    if expires and dt.date.today() > expires:
        raise LicenseError(f"This license expired on {expires:%d %b %Y}. Contact the seller to renew it.")
    return {"customer": customer, "expires": expires.isoformat() if expires else None}


# ---------------------------------------------------------------- the stored key
_cache: dict = {}


def status() -> dict:
    """{"ok": bool, ...} for the key saved on this PC (cached until the file changes or the day changes)."""
    try:
        stamp = (os.path.getmtime(KEY_FILE), dt.date.today())
    except OSError:
        return {"ok": False, "error": ""}
    if _cache.get("stamp") != stamp:
        try:
            with open(KEY_FILE, encoding="utf-8") as f:
                _cache.update(stamp=stamp, result={"ok": True, **check_key(f.read())})
        except LicenseError as e:
            _cache.update(stamp=stamp, result={"ok": False, "error": str(e)})
    return _cache["result"]


# ---------------------------------------------------------------- activation page + gate
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activate Creator Studio</title><link rel="icon" href="/static/icon.png"><style>
:root{--bg:#0d0e11;--panel:#16181d;--panel2:#1d2027;--line:#2a2e37;--text:#eceef2;--dim:#9aa1ad;--accent:#ffd60a;--red:#ff5a5a;--green:#3ddc84}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font:15px/1.5 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:480px;margin:0 auto;padding:10vh 16px 40px}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px;margin-bottom:22px}
.logo i{width:26px;height:26px;border-radius:7px;background:linear-gradient(135deg,#f0443e,#ffd60a)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px}
h1{font-size:20px;margin:0 0 6px} p{color:var(--dim);margin:0 0 14px;font-size:14px}
ol{color:var(--dim);font-size:14px;padding-left:20px;margin:0 0 16px} li{margin:4px 0}
.code{display:flex;gap:8px;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin-bottom:18px}
.code b{flex:1;font:600 17px/1.3 Consolas,monospace;letter-spacing:.06em;word-break:break-all}
label{display:block;font-size:13px;color:var(--dim);margin:0 0 4px}
textarea{width:100%;min-height:92px;background:var(--panel2);border:1px solid var(--line);border-radius:9px;color:var(--text);padding:10px 12px;font:14px/1.4 Consolas,monospace;resize:vertical}
textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}
button{font:inherit;cursor:pointer;border-radius:9px;padding:8px 14px;border:1px solid var(--line);background:var(--panel2);color:var(--text)}
button.primary{background:var(--accent);color:#1a1500;border-color:var(--accent);font-weight:700;width:100%;margin-top:14px;padding:10px}
button:disabled{opacity:.5}
.msg{font-size:13px;margin-top:12px;min-height:1em;color:var(--red)} .msg.ok{color:var(--green)}
</style></head><body><div class="wrap">
<div class="logo"><i></i>Creator Studio</div>
<div class="card">
  <h1>Activate Creator Studio</h1>
  <ol><li>Copy your machine code below and send it to the seller with your purchase details.</li>
      <li>Paste the license key you receive and click Activate.</li></ol>
  <label>Your machine code</label>
  <div class="code"><b id="mc">{{ code }}</b><button id="copy" type="button">Copy</button></div>
  <label for="key">License key</label>
  <textarea id="key" placeholder="Paste your license key here" spellcheck="false"></textarea>
  <div class="msg" id="msg">{{ error }}</div>
  <button class="primary" id="go">Activate</button>
</div></div>
<script>
const $ = s => document.querySelector(s);
$("#copy").onclick = async () => {
  try { await navigator.clipboard.writeText($("#mc").textContent); $("#copy").textContent = "Copied"; }
  catch { getSelection().selectAllChildren($("#mc")); }
  setTimeout(() => $("#copy").textContent = "Copy", 1500);
};
$("#go").onclick = async () => {
  $("#go").disabled = true; $("#msg").className = "msg"; $("#msg").textContent = "";
  try {
    const r = await fetch("/api/license", {method: "POST", headers: {"Content-Type": "application/json"},
                                           body: JSON.stringify({key: $("#key").value})});
    const d = await r.json();
    if (!r.ok) { $("#msg").textContent = d.error; return; }
    $("#msg").className = "msg ok"; $("#msg").textContent = "Activated. Opening Creator Studio...";
    setTimeout(() => location.href = "/", 700);
  } catch { $("#msg").textContent = "Couldn't reach Creator Studio. Is it still running?"; }
  finally { $("#go").disabled = false; }
};
</script></body></html>"""


def init(app):
    @app.before_request
    def require_license():
        if request.path in PUBLIC_PATHS or status()["ok"]:
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="Creator Studio isn't activated on this PC."), 403
        return redirect("/activate")

    @app.get("/activate")
    def activate_page():
        st = status()
        if st["ok"]:
            return redirect("/")
        return render_template_string(PAGE, code=machine_code(), error=st.get("error", ""))

    @app.get("/api/license")
    def license_info():
        return jsonify(machine_code=machine_code(), **status())

    @app.post("/api/license")
    def activate():
        key = (request.get_json(force=True).get("key") or "").strip()
        try:
            info = check_key(key)
        except LicenseError as e:
            return jsonify(error=str(e)), 400
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        _cache.clear()
        return jsonify(ok=True, **info)
