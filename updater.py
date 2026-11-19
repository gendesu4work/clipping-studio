"""Auto-updater: pulls the latest Creator Studio from GitHub every time the app opens.

Two ways to update, picked automatically:
- git install (folder has .git): fetch + fast-forward to origin/main. Uses your normal git sign-in,
  so it works with a private repo. Skipped if you have uncommitted edits or local commits,
  so it never overwrites work in progress.
- plain install (unzipped copy): downloads the main branch zip and copies the new files over.
  Your own data (settings, sign-ins, license, library, jobs, outputs, fonts) is never touched.
  A private repo needs a token: put "update_token": "<github token>" in settings.json
  or set CREATOR_STUDIO_UPDATE_TOKEN.

Turn it off with "auto_update": false in settings.json. Any failure (offline, GitHub down)
is logged and the app just starts with what it has.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

OWNER, REPO, BRANCH = "gendesu4work", "clipping-studio", "main"
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, ".update_version")
NO_WINDOW = 0x08000000

# never overwritten by a zip update (personal data / runtime state)
PROTECTED = {
    ".env", "cookies.txt", "config.json", "settings.json", "license.key", "signing_key.pem",
    "studio.log", ".update_version", ".installed_requirements",
}
PROTECTED_DIRS = {"jobs", "library", "outputs", "fonts", ".git", ".venv", "venv", "__pycache__"}


def _log(msg: str) -> None:
    try:
        with open(os.path.join(HERE, "studio.log"), "a", encoding="utf-8") as f:
            f.write(f"[updater] {msg}\n")
    except Exception:
        pass


def _settings() -> dict:
    try:
        with open(os.path.join(HERE, "settings.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _run(args, timeout=30):
    return subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                          timeout=timeout, creationflags=NO_WINDOW)


def _file_hash(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- git install
def _update_git() -> bool:
    if _run(["git", "fetch", "--quiet", "origin", BRANCH], timeout=30).returncode != 0:
        _log("git fetch failed (offline?), skipping update")
        return False
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote = _run(["git", "rev-parse", f"origin/{BRANCH}"]).stdout.strip()
    if not head or not remote or head == remote:
        return False
    if _run(["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{BRANCH}"]).returncode != 0:
        _log("local commits not on GitHub yet, skipping update")
        return False
    if _run(["git", "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        _log("uncommitted changes in the app folder, skipping update")
        return False
    r = _run(["git", "merge", "--ff-only", "--quiet", f"origin/{BRANCH}"], timeout=60)
    if r.returncode != 0:
        _log(f"git update failed: {r.stderr.strip()}")
        return False
    _log(f"updated {head[:7]} -> {remote[:7]}")
    return True


# ---------------------------------------------------------------- zip install
def _get(url: str, token: str | None, timeout: int):
    req = urllib.request.Request(url, headers={"User-Agent": "creator-studio-updater",
                                               "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout).read()


def _protected(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    return parts[0] in PROTECTED_DIRS or (len(parts) == 1 and parts[0] in PROTECTED)


def _update_zip(token: str | None) -> bool:
    api = f"https://api.github.com/repos/{OWNER}/{REPO}"
    try:
        sha = json.loads(_get(f"{api}/commits/{BRANCH}", token, 10))["sha"]
    except Exception as e:
        _log(f"couldn't check GitHub for updates ({e}), skipping")
        return False
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            if f.read().strip() == sha:
                return False
    except OSError:
        pass
    try:
        blob = _get(f"{api}/zipball/{sha}", token, 120)
    except Exception as e:
        _log(f"download failed ({e}), skipping")
        return False

    changed = 0
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(io.BytesIO(blob)).extractall(tmp)
        roots = os.listdir(tmp)
        if len(roots) != 1:
            _log("unexpected zip layout, skipping")
            return False
        src_root = os.path.join(tmp, roots[0])
        for dirpath, _dirs, files in os.walk(src_root):
            for name in files:
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, src_root)
                if _protected(rel):
                    continue
                dst = os.path.join(HERE, rel)
                if _file_hash(src) == _file_hash(dst):
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                changed += 1
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(sha)
    _log(f"updated to {sha[:7]} ({changed} files changed)")
    return changed > 0


# ---------------------------------------------------------------- entry point
def _python_exe() -> str:
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(console):
            return console
    return exe


def update() -> bool:
    """Bring the app up to date. Returns True if any files changed."""
    s = _settings()
    if s.get("auto_update") is False:
        return False
    req_file = os.path.join(HERE, "requirements.txt")
    try:
        if os.path.isdir(os.path.join(HERE, ".git")) and shutil.which("git"):
            changed = _update_git()
        else:
            token = os.environ.get("CREATOR_STUDIO_UPDATE_TOKEN") or s.get("update_token")
            changed = _update_zip(token)
    except Exception as e:
        _log(f"update error: {e}")
        return False
    _install_packages(req_file)
    return changed


REQS_DONE_FILE = os.path.join(HERE, ".installed_requirements")


def _install_packages(req_file: str) -> None:
    """pip install whenever requirements.txt differs from the last one that installed OK,
    so a failed or interrupted install is retried on the next launch."""
    want = _file_hash(req_file)
    try:
        with open(REQS_DONE_FILE, encoding="utf-8") as f:
            if f.read().strip() == want:
                return
    except OSError:
        pass
    _log("requirements.txt changed, installing packages")
    try:
        r = _run([_python_exe(), "-m", "pip", "install", "-q", "-r", req_file], timeout=900)
        if r.returncode == 0:
            with open(REQS_DONE_FILE, "w", encoding="utf-8") as f:
                f.write(want or "")
            _log("packages installed")
        else:
            _log(f"pip failed: {r.stderr.strip()[-500:]}")
    except Exception as e:
        _log(f"pip failed: {e}")


if __name__ == "__main__":
    print("updated" if update() else "already up to date (or skipped, see studio.log)")
