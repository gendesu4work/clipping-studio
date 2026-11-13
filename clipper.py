"""YouTube Clipper (Creator Studio tab) - paste a link, pick start/end, download the clip.

Run:  python app.py   then open http://127.0.0.1:5000
Needs: flask, yt-dlp, and ffmpeg on PATH.
"""
import io
import json
import os
import time
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func
from flask import Flask, jsonify, request, send_file, send_from_directory
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

app = Flask(__name__, static_folder="static")

QUALITY_FORMATS = {
    "best": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
    "1080": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]",
    "audio": "ba/b",
}


def parse_time(value: str) -> float:
    """Accept '83', '1:23', '01:02:03', or '1:23.5' and return seconds."""
    value = (value or "").strip()
    if not re.fullmatch(r"\d+(\.\d+)?|(\d+:){1,2}\d{1,2}(\.\d+)?", value):
        raise ValueError(f"Invalid time: '{value}'. Use seconds or mm:ss or hh:mm:ss.")
    seconds = 0.0
    for part in value.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", text).strip()[:80] or "clip"


def fmt(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


# --------------------------------------------- YouTube sign-in (cookies) ----
# Age-restricted / members-only videos need a signed-in YouTube session.
COOKIE_FILE = Path(__file__).with_name("cookies.txt")
SETTINGS_FILE = Path(__file__).with_name("settings.json")
BROWSERS = ("brave", "edge", "chrome", "firefox", "opera", "vivaldi")


def settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(**kw):
    s = settings()
    s.update(kw)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def cookie_source() -> str:
    src = settings().get("cookies")
    if src:
        return src
    return "file" if COOKIE_FILE.exists() else "none"


def deno_path():
    """YouTube needs a JavaScript runtime (Deno) to unlock most video formats."""
    found = shutil.which("deno")
    if found:
        return found
    try:
        import deno
        return deno.find_deno_bin()
    except Exception:
        return None


DENO = deno_path()


def ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if DENO:
        opts["js_runtimes"] = {"deno": {"path": DENO}}
    src = cookie_source()
    if src == "file" and COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    elif src in BROWSERS:
        opts["cookiesfrombrowser"] = (src,)
    opts.update(extra)
    return opts


def friendly_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    low = msg.lower()
    src = cookie_source()
    if "not a bot" in low:
        return "YouTube thinks this is a bot. Connect your YouTube sign-in below and try again."
    if "sign in to confirm" in low or ("age" in low and "inappropriate" in low) or "members-only" in low or "private video" in low:
        if src == "none":
            return ("This video needs a signed-in YouTube account (age-restricted or private). "
                    "Open “YouTube sign-in” below and connect your browser or a cookies.txt file.")
        return (f"YouTube still refused with your sign-in ({src}). Make sure you're signed in to YouTube "
                "there, or try the cookies.txt option instead.")
    if "could not copy" in low and "cookie" in low or "database is locked" in low:
        return (f"Couldn't read cookies because {src} is open and locks them. Fully close {src} "
                "(including in the system tray) and try again, or use the cookies.txt option.")
    if "decrypt" in low or "app-bound" in low or "dpapi" in low:
        return (f"{src.title()} encrypts its cookies so other programs can't read them. "
                "Use the cookies.txt option instead (takes 1 minute).")
    return msg


@app.get("/api/cookies")
def cookies_get():
    return jsonify(source=cookie_source(), has_file=COOKIE_FILE.exists(), browsers=BROWSERS)


@app.post("/api/cookies")
def cookies_set():
    body = request.json or {}
    src = body.get("source", "none")
    if src not in ("none", "file", *BROWSERS):
        return jsonify(error="Unknown option."), 400
    if "text" in body:  # uploaded cookies.txt contents
        text = body["text"] or ""
        if "youtube.com" not in text or "\t" not in text:
            return jsonify(error="That doesn't look like a YouTube cookies.txt (Netscape format) file."), 400
        if not text.lstrip().startswith("#"):
            text = "# Netscape HTTP Cookie File\n" + text
        COOKIE_FILE.write_text(text, encoding="utf-8")
        src = "file"
    if src == "file" and not COOKIE_FILE.exists():
        return jsonify(error="Upload a cookies.txt file first."), 400
    save_settings(cookies=src)
    if src == "none":
        return jsonify(source=src, ok=True)
    # quick check that the cookies can actually be read
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            ydl.cookiejar  # forces cookie loading
        return jsonify(source=src, ok=True)
    except Exception as e:
        return jsonify(source=src, ok=False, error=friendly_error(e))


# ---------------------------------------------------------------- Gemini ----
ENV_FILE = Path(__file__).with_name(".env")
_model_cache: dict = {}


def load_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("GEMINI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"')
    return key


def gemini_client():
    key = load_key()
    if not key:
        raise RuntimeError("No Gemini API key yet. Add it in the Gemini settings box.")
    # 5-minute cap per request so an overloaded model can't hang the page forever
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=300_000))


def pro_blocked() -> bool:
    """True once Google told us this key has no Pro quota (free-tier keys get limit 0)."""
    return settings().get("pro_blocked_key") == load_key()[-6:]


def model_order(client) -> list[str]:
    """Models to try, best first: Pro, then Flash as a fallback when Pro has no quota."""
    if os.environ.get("GEMINI_MODEL"):
        return [os.environ["GEMINI_MODEL"]]
    order = [] if pro_blocked() else [best_model(client, "pro")]
    try:
        best_model(client, "flash")
        order += _model_cache[(load_key(), "flash-all")][:3]  # newest Flash + two older backups
    except RuntimeError:
        pass
    return order or [best_model(client, "pro")]


def gemini_error(e: Exception, model: str = "") -> str:
    msg = str(e)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        wait = re.search(r"retry in ([\dhms.]+?)(?:\d+s)?\.", msg) or re.search(r"retry in (\S+?)\.", msg)
        when = f" It resets in about {wait.group(1).split('.')[0]}." if wait else ""
        return (f"Gemini's free usage limit for {model or 'this model'} is used up.{when} "
                "To remove the limit, turn on billing for your API key at aistudio.google.com.")
    if any(s in msg for s in ("503", "UNAVAILABLE", "high demand", "504", "DEADLINE", "timed out", "ReadTimeout")):
        return f"Gemini ({model or 'the model'}) is overloaded right now. Wait a minute and try again."
    if "API key not valid" in msg or "PERMISSION_DENIED" in msg:
        return "Gemini rejected the API key. Paste a new key in the Gemini box."
    return msg[:400]


def best_model(client, family: str = "pro") -> str:
    """Pick the newest Gemini model of a family ('pro' or 'flash') this key can use."""
    if os.environ.get("GEMINI_MODEL"):
        return os.environ["GEMINI_MODEL"]
    cache_key = (load_key(), family)
    if cache_key in _model_cache:
        return _model_cache[cache_key]
    skip = ("tts", "image", "embedding", "audio", "computer", "customtools", "live", "learnlm", "vision", "lite", "thinking")
    candidates = []
    for m in client.models.list():
        name = m.name.removeprefix("models/")
        actions = getattr(m, "supported_actions", None) or []
        if f"-{family}" not in name or not name.startswith("gemini") or any(s in name for s in skip):
            continue
        if actions and "generateContent" not in actions:
            continue
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        version = float(ver.group(1)) if ver else 0
        stable = "preview" not in name and "exp" not in name
        latest_alias = name.endswith("-latest")
        # newest version first; then prefer '-latest' alias, then stable, then shorter name
        candidates.append((version, latest_alias, stable, -len(name), name))
    if not candidates:
        raise RuntimeError(f"Your API key has no Gemini {family.title()} model available.")
    ranked = [c[-1] for c in sorted(candidates, reverse=True)]
    # keep one model per version number for the backup list (e.g. 3.8-flash, then 3.5-flash)
    seen, distinct = set(), []
    for c in sorted(candidates, reverse=True):
        if c[0] not in seen:
            seen.add(c[0])
            distinct.append(c[-1])
    model = ranked[0]
    _model_cache[cache_key] = model
    _model_cache[(load_key(), family + "-all")] = distinct
    return model


class Moment(BaseModel):
    start: str = Field(description="Start timestamp, MM:SS or H:MM:SS")
    end: str = Field(description="End timestamp, MM:SS or H:MM:SS")
    title: str = Field(description="Short catchy title, max 8 words")
    description: str = Field(description="What happens / what people do and say in this moment, 1-3 sentences")
    why: str = Field(description="Why this moment is great or funny, one sentence")
    score: int = Field(description="How strong this moment is, 1-10")


class MomentList(BaseModel):
    summary: str = Field(description="One-sentence summary of the whole video")
    moments: list[Moment]


MODES = {
    "best": "the BEST, most engaging, highlight-worthy moments (the parts people would share or rewatch)",
    "funny": "the FUNNIEST moments (jokes, bloopers, awkward or hilarious reactions, comedic timing)",
}


def youtube_watch_url(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", url)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else url


@app.get("/api/gemini/status")
def gemini_status():
    key = load_key()
    if not key:
        return jsonify(connected=False)
    try:
        order = model_order(genai.Client(api_key=key))
        note = " (Pro needs billing)" if pro_blocked() else ""
        return jsonify(connected=True, model=order[0] + note)
    except Exception as e:
        return jsonify(connected=False, error=str(e))


@app.post("/api/gemini/key")
def gemini_key():
    key = ((request.json or {}).get("key") or "").strip()
    if not key:
        return jsonify(error="Paste your API key."), 400
    try:
        model = best_model(genai.Client(api_key=key))
    except Exception as e:
        return jsonify(error=f"That key didn't work: {e}"), 400
    ENV_FILE.write_text(f"GEMINI_API_KEY={key}\n", encoding="utf-8")
    os.environ["GEMINI_API_KEY"] = key
    save_settings(pro_blocked_key=None)  # new key -> try Pro again
    return jsonify(connected=True, model=model)


@app.post("/api/moments")
def moments():
    body = request.json or {}
    url = (body.get("url") or "").strip()
    mode = body.get("mode", "best")
    extra = (body.get("extra") or "").strip()[:500]
    count = max(1, min(int(body.get("count") or 5), 15))
    duration = body.get("duration") or 0
    if not url:
        return jsonify(error="Paste a YouTube link first."), 400
    try:
        client = gemini_client()
        models = model_order(client)
    except Exception as e:
        return jsonify(error=gemini_error(e)), 400
    used = {"model": models[0], "note": ""}

    what = MODES.get(mode, MODES["best"])
    if extra:
        what += f". The user also asks: {extra}"
    prompt = (
        f"Watch this whole video carefully (picture AND audio) and find up to {count} of {what}.\n"
        "For each moment give exact start and end timestamps from the video timeline so it works as a "
        "standalone clip: start a second or two before the moment builds up and end after the payoff/reaction. "
        "Each clip should usually be 5-60 seconds. Don't overlap moments. "
        "Describe concretely what the people do and say. Sort by score, best first."
        + (f" The video is {int(duration)} seconds long; never go past that." if duration else "")
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=MomentList,
        # long videos use fewer tokens per frame so they fit in the context window
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW if duration and duration > 1200 else None,
    )
    # Sample fewer frames on long videos so they fit the token limits (esp. free tier).
    fps = 1.0 if duration <= 600 else 0.5 if duration <= 1800 else 0.2

    def ask(file_uri, mime):
        part = types.Part(file_data=types.FileData(file_uri=file_uri, mime_type=mime),
                          video_metadata=types.VideoMetadata(fps=fps))
        last = None
        def call(model):
            # Google returns 503/500 when a model is overloaded; retry once, then try the next model.
            for attempt in range(2):
                try:
                    return client.models.generate_content(
                        model=model,
                        contents=types.Content(parts=[part, types.Part(text=prompt)]),
                        config=config,
                    )
                except Exception as e:
                    busy = any(s in str(e) for s in ("503", "UNAVAILABLE", "500 INTERNAL", "overloaded"))
                    if not busy or attempt == 1:
                        raise
                    time.sleep(5)

        for i, model in enumerate(models):
            try:
                resp = call(model)
                used["model"] = model
                return resp.parsed if resp.parsed else MomentList.model_validate_json(resp.text)
            except Exception as e:
                last = gemini_error(e, model)
                quota = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
                busy = "overloaded" in last
                if quota and "-pro" in model and "limit: 0" in str(e):
                    save_settings(pro_blocked_key=load_key()[-6:])  # free-tier key: Pro never available
                if not (quota or busy) or i == len(models) - 1:
                    raise RuntimeError(last) from e
                reason = "has no free quota left" if quota else "was overloaded"
                used["note"] = f"{model} {reason}, so a backup model was used."
        raise RuntimeError(last or "No model available.")

    result, method = None, "youtube"
    if not body.get("restricted"):
        try:
            result = ask(youtube_watch_url(url), "video/youtube")
        except Exception as e:
            print("Gemini YouTube-link analysis failed, falling back to upload:", e)
            if "usage limit" in str(e) or "overloaded" in str(e):  # Gemini problem, not a video problem
                return jsonify(error=str(e)), 503
    if result is None:
        # Gemini can't open age-restricted/private videos by link, so download a small
        # copy with the user's YouTube sign-in and upload it to Gemini instead.
        method = "upload"
        try:
            result = analyze_by_upload(client, url, ask)
        except Exception as e:
            msg = str(e) if ("usage limit" in str(e) or "overloaded" in str(e)) else friendly_error(e)
            return jsonify(error=f"Gemini couldn't analyze this video: {msg}"), 502
    if result is None:
        return jsonify(error="Gemini returned nothing. Try again."), 502

    clean = []
    for m in result.moments:
        try:
            s, e = parse_time(m.start), parse_time(m.end)
        except ValueError:
            continue
        if duration:
            e = min(e, float(duration))
        if e - s >= 1:
            clean.append({**m.model_dump(), "start_sec": s, "end_sec": e})
    return jsonify(model=used["model"], note=used["note"], summary=result.summary, moments=clean, method=method)


def analyze_by_upload(client, url, ask):
    uploaded = None
    try:
        path, _ = full_download(url, "360")
        uploaded = client.files.upload(file=str(path), config={"mime_type": "video/mp4"})
        for _ in range(180):  # wait up to ~6 min for Gemini to process the video
            if uploaded.state and uploaded.state.name == "ACTIVE":
                break
            if uploaded.state and uploaded.state.name == "FAILED":
                raise RuntimeError("Gemini failed to process the uploaded video.")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        return ask(uploaded.uri, "video/mp4")
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


@app.get("/clipper")
def index():
    return send_from_directory(app.static_folder, "clipper.html")


@app.post("/api/info")
def info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Please paste a YouTube link."), 400
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=f"Couldn't read that video: {friendly_error(e)}"), 400
    _meta_cache[video_id(url)] = {
        "title": data.get("title") or "clip",
        "restricted": (data.get("age_limit") or 0) >= 18 or data.get("availability") not in (None, "public"),
    }
    return jsonify(
        title=data.get("title"),
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        channel=data.get("uploader"),
        id=data.get("id"),
        restricted=(data.get("age_limit") or 0) >= 18 or data.get("availability") not in (None, "public"),
    )


@app.post("/api/clip")
def clip():
    body = request.json or {}
    url = body.get("url", "").strip()
    quality = body.get("quality", "720")
    if not url:
        return jsonify(error="Please paste a YouTube link."), 400
    try:
        start = parse_time(body.get("start"))
        end = parse_time(body.get("end"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if end <= start:
        return jsonify(error="End time must be after start time."), 400

    if quality not in QUALITY_FORMATS:
        quality = "720"
    audio_only = quality == "audio"
    workdir = Path(tempfile.mkdtemp(prefix="ytclip_"))
    try:
        meta = video_meta(url)
        out = None
        # Fast path: let yt-dlp/ffmpeg fetch only the requested part. YouTube sometimes
        # stalls this (often on restricted videos), so it runs with a time limit.
        if not meta["restricted"]:
            out = range_download(url, quality, start, end, workdir)
        if out is None:
            # Reliable path: download the whole video once (cached), then cut locally.
            src, _ = full_download(url, quality)
            out = cut_local(src, start, end, audio_only, workdir)
        payload = io.BytesIO(out.read_bytes())
        name = f"{safe_name(meta['title'])} [{fmt(start)}-{fmt(end)}]{out.suffix}"
        lib_name = None
        try:  # keep a copy in the Studio library so ClipForge can pick it up
            from forge_routes import save_to_library
            lib_name = save_to_library(out, name)
        except Exception as e:
            print("Could not save clip to library:", e)
    except Exception as e:
        print("Clip failed:", e)
        return jsonify(error=f"Clipping failed: {friendly_error(e)}"), 500
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    resp = send_file(payload, as_attachment=True, download_name=name)
    if lib_name:
        from urllib.parse import quote
        resp.headers["X-Library-File"] = quote(lib_name)
        resp.headers["Access-Control-Expose-Headers"] = "X-Library-File, Content-Disposition"
    return resp


# ------------------------------------------------------- clip helpers ----
CACHE_DIR = Path(tempfile.gettempdir()) / "ytclipper_cache"
CACHE_HOURS = 3
_meta_cache: dict = {}
_locks: dict = {}
_locks_guard = threading.Lock()


def video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", url)
    return m.group(1) if m else re.sub(r"\W", "_", url)[-40:]


def video_meta(url: str) -> dict:
    vid = video_id(url)
    if vid not in _meta_cache:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
        _meta_cache[vid] = {
            "title": data.get("title") or "clip",
            "restricted": (data.get("age_limit") or 0) >= 18 or data.get("availability") not in (None, "public"),
        }
    return _meta_cache[vid]


def cli_auth_args() -> list[str]:
    args = []
    src = cookie_source()
    if src == "file" and COOKIE_FILE.exists():
        args += ["--cookies", str(COOKIE_FILE)]
    elif src in BROWSERS:
        args += ["--cookies-from-browser", src]
    if DENO:
        args += ["--js-runtimes", f"deno:{DENO}"]
    return args


def range_download(url, quality, start, end, workdir: Path):
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
           "-f", QUALITY_FORMATS[quality], "--download-sections", f"*{start}-{end}",
           "--force-keyframes-at-cuts", "-o", str(workdir / "clip.%(ext)s"), *cli_auth_args()]
    if quality == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
    else:
        cmd += ["--merge-output-format", "mp4"]
    cmd.append(url)
    timeout = min(240, 60 + 3 * (end - start))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        print(f"Range download stalled after {timeout:.0f}s; using full download instead.")
        return None
    files = [p for p in workdir.iterdir() if p.is_file() and p.stat().st_size > 0 and ".part" not in p.name]
    if proc.returncode != 0 or not files:
        print("Range download failed; using full download instead:", (err or b"").decode(errors="replace")[-300:])
        for p in workdir.iterdir():
            p.unlink(missing_ok=True)
        return None
    return max(files, key=lambda p: p.stat().st_size)


def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def clean_cache():
    CACHE_DIR.mkdir(exist_ok=True)
    cutoff = time.time() - CACHE_HOURS * 3600
    for p in CACHE_DIR.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def full_download(url, quality):
    """Download the whole video once per (video, quality) and reuse it for later clips."""
    clean_cache()
    vid = video_id(url)
    key = f"{vid}_{quality}"
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        done = [p for p in CACHE_DIR.glob(key + ".*") if p.suffix not in (".part", ".ytdl") and ".f" not in p.stem[len(key):]]
        if done:
            done[0].touch()
            return done[0], vid
        fmt_spec = "18/b[height<=360][ext=mp4]/bv*[height<=360]+ba/b" if quality == "360" else QUALITY_FORMATS[quality]
        opts = ydl_opts(format=fmt_spec, outtmpl=str(CACHE_DIR / f"{key}.%(ext)s"),
                        http_chunk_size=10 * 1024 * 1024)
        if quality != "audio":
            opts["merge_output_format"] = "mp4" if quality == "360" else "mkv"
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
        done = [p for p in CACHE_DIR.glob(key + ".*") if p.suffix not in (".part", ".ytdl") and ".f" not in p.stem[len(key):]]
        if not done:
            raise RuntimeError("Download produced no file.")
        return done[0], vid


def cut_local(src: Path, start, end, audio_only, workdir: Path) -> Path:
    if audio_only:
        out = workdir / "clip.mp3"
        codec = ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
    else:
        out = workdir / "clip.mp4"
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start}", "-i", str(src),
           "-t", f"{end - start}", *codec, str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("ffmpeg failed to cut the clip: " + r.stderr[-300:])
    return out


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
