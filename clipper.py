"""Clip downloader (Creator Studio tab) - paste a YouTube or Twitch link, pick start/end, download the clip.

Run:  python app.py   then open http://127.0.0.1:5000
Needs: flask, yt-dlp, and ffmpeg on PATH.
"""
import copy
import io
import json
import struct
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

from ffmpeg_tools import ensure_ffmpeg

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
    if "requested format is not available" in low:
        if not DENO:
            return ("YouTube only offered preview images because the video helper (Deno) is missing. "
                    "Restart Creator Studio so it can finish installing packages, then try again.")
        return ("YouTube didn't offer any downloadable video for this link. Restart Creator Studio to "
                "get the latest downloader, or connect your YouTube sign-in below and try again.")
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


def retired_models() -> set[str]:
    """Models Google listed but then refused with 404 (e.g. 'no longer available to new users')."""
    return set(settings().get("retired_models") or [])


def is_retired_error(e: Exception, model: str) -> bool:
    """A 404 about the model itself (not about the video)."""
    msg = str(e)
    return ("404" in msg or "NOT_FOUND" in msg) and (f"models/{model}" in msg or "no longer available" in msg)


def retire_model(model: str):
    save_settings(retired_models=sorted(retired_models() | {model}))
    _model_cache.clear()  # re-pick the newest model without the retired one


def model_order(client) -> list[str]:
    """Models to try, best first: Pro, then Flash as a fallback when Pro has no quota."""
    if os.environ.get("GEMINI_MODEL"):
        return [os.environ["GEMINI_MODEL"]]
    order = []
    if not pro_blocked():
        try:
            order.append(best_model(client, "pro"))
        except RuntimeError:
            pass  # no usable Pro model for this key: Flash only
    try:
        best_model(client, "flash")
        order += _model_cache[(load_key(), "flash-all")][:3]  # newest Flash + two older backups
    except RuntimeError:
        pass
    order += lite_models(client)[:1]  # Flash-Lite: lower quality but usually free when the others are overloaded
    return order or [best_model(client, "pro")]


def lite_models(client) -> list[str]:
    """Newest Flash-Lite models, used only as a last resort."""
    cache_key = (load_key(), "flash-lite")
    if cache_key not in _model_cache:
        names = []
        try:
            for m in client.models.list():
                name = m.name.removeprefix("models/")
                if name.startswith("gemini") and "flash-lite" in name and not any(
                        s in name for s in ("tts", "image", "audio", "live")):
                    ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
                    names.append((float(ver.group(1)) if ver else 0, "preview" not in name, -len(name), name))
        except Exception:
            pass
        _model_cache[cache_key] = [n[-1] for n in sorted(names, reverse=True)]
    return _model_cache[cache_key]


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
    retired = retired_models()
    for m in client.models.list():
        name = m.name.removeprefix("models/")
        actions = getattr(m, "supported_actions", None) or []
        if name in retired:
            continue
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


def platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "twitch.tv" in u:
        return "twitch"
    return "other"


def twitch_id(url: str) -> str | None:
    """Clip slug or VOD number from any Twitch clip/VOD link."""
    m = (re.search(r"clips\.twitch\.tv/(?:embed\?clip=)?([\w-]+)", url)
         or re.search(r"twitch\.tv/[\w]+/clip/([\w-]+)", url)
         or re.search(r"twitch\.tv/videos/(\d+)", url))
    return m.group(1) if m else None


def preview_source(data: dict):
    """A URL the page's own video player can play (Twitch has no embeddable player on localhost)."""
    fmts = [f for f in data.get("formats") or [] if f.get("vcodec") != "none" and f.get("url")]
    mp4 = [f for f in fmts if f.get("ext") == "mp4" and (f.get("protocol") or "").startswith("http")]
    hls = [f for f in fmts if "m3u8" in (f.get("protocol") or "")]
    def pick(fs): return min(fs, key=lambda f: abs((f.get("height") or 720) - 720))
    if mp4: return pick(mp4)["url"], "mp4"
    if hls: return pick(hls)["url"], "hls"
    return (data.get("url"), "mp4") if data.get("url") else (None, None)


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
        client = genai.Client(api_key=key)
        try:
            model = best_model(client, "pro")
        except RuntimeError:
            model = best_model(client, "flash")
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
        return jsonify(error="Paste a YouTube or Twitch link first."), 400
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
            # Google answers 503/500 fast when a model is overloaded: back off and retry the same model.
            # Timeouts already cost minutes, so those go straight to the next model instead.
            waits = (5, 15)
            for attempt in range(len(waits) + 1):
                try:
                    return client.models.generate_content(
                        model=model,
                        contents=types.Content(parts=[part, types.Part(text=prompt)]),
                        config=config,
                    )
                except Exception as e:
                    busy = any(s in str(e) for s in ("503", "UNAVAILABLE", "500 INTERNAL", "overloaded", "high demand"))
                    if not busy or attempt == len(waits):
                        raise
                    time.sleep(waits[attempt])

        # Try every model; if some were only overloaded (not out of quota), wait and give those one more round.
        todo = list(models)
        for round_no in range(2):
            busy_models = []
            for model in todo:
                try:
                    resp = call(model)
                    used["model"] = model
                    if model != models[0] and not used["note"]:
                        used["note"] = f"{models[0]} was busy, so a backup model was used."
                    return resp.parsed if resp.parsed else MomentList.model_validate_json(resp.text)
                except Exception as e:
                    last = gemini_error(e, model)
                    quota = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
                    busy = "overloaded" in last
                    retired = is_retired_error(e, model)
                    if quota and "-pro" in model and "limit: 0" in str(e):
                        save_settings(pro_blocked_key=load_key()[-6:])  # free-tier key: Pro never available
                    if retired:
                        retire_model(model)  # Google listed it but refuses it: never pick it again
                        last = f"{model} is no longer available to this API key."
                    if not (quota or busy or retired):
                        raise RuntimeError(last) from e
                    if busy:
                        busy_models.append(model)
                    print(f"Gemini {model}: {'no quota left' if quota else 'overloaded' if busy else 'retired'}, trying next")
            if not busy_models or round_no == 1:
                break
            todo = busy_models
            time.sleep(30)
        raise RuntimeError(last or "No model available.")

    result, method = None, "youtube"
    if not body.get("restricted") and platform(url) == "youtube":
        try:
            result = ask(youtube_watch_url(url), "video/youtube")
        except Exception as e:
            print("Gemini YouTube-link analysis failed, falling back to upload:", e)
            if "usage limit" in str(e) or "overloaded" in str(e):  # Gemini problem, not a video problem
                return jsonify(error=str(e)), 503
    if result is None:
        # Gemini can only open public YouTube links, so for Twitch and restricted videos
        # download a small copy (with the user's sign-in if needed) and upload it instead.
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
        return jsonify(error="Please paste a YouTube or Twitch link."), 400
    try:
        data = video_info(url, fresh=True)
    except Exception as e:
        return jsonify(error=f"Couldn't read that video: {friendly_error(e)}"), 400
    _meta_cache[video_id(url)] = {
        "title": data.get("title") or "clip",
        "restricted": (data.get("age_limit") or 0) >= 18 or data.get("availability") not in (None, "public"),
    }
    plat = platform(url)
    preview, preview_type = preview_source(data) if plat != "youtube" else (None, None)
    return jsonify(
        title=data.get("title"),
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        channel=(data.get("channel") or data.get("creator") or data.get("uploader")) if plat == "twitch" else data.get("uploader"),
        id=data.get("id"),
        platform=plat,
        kind="clip" if plat == "twitch" and "/videos/" not in url else "video",
        preview=preview, preview_type=preview_type,
        restricted=(data.get("age_limit") or 0) >= 18 or data.get("availability") not in (None, "public"),
    )


@app.post("/api/twitch/download")
def twitch_download():
    """Twitch clips tab: paste a clip link, get the whole clip in the library, ready for ClipForge."""
    url = ((request.json or {}).get("url") or "").strip()
    if platform(url) != "twitch" or not twitch_id(url):
        return jsonify(error="That isn't a Twitch clip link. It should look like twitch.tv/<channel>/clip/<name>."), 400
    if "/videos/" in url:
        return jsonify(error="That's a past broadcast, not a clip. Use the Clip downloader tab to cut a part of it."), 400
    url = url.split("?")[0]   # drop tracking bits like ?range=7d
    workdir = Path(tempfile.mkdtemp(prefix="twclip_"))
    try:
        with yt_dlp.YoutubeDL(ydl_opts(format=QUALITY_FORMATS["best"], merge_output_format="mp4",
                                       outtmpl=str(workdir / "clip.%(ext)s"))) as ydl:
            data = ydl.extract_info(url, download=True)
        files = [p for p in workdir.iterdir() if p.is_file() and p.stat().st_size > 0 and ".part" not in p.name]
        if not files:
            raise RuntimeError("The download produced no file.")
        out = max(files, key=lambda p: p.stat().st_size)
        title = data.get("title") or twitch_id(url)
        channel = data.get("channel") or data.get("creator") or data.get("uploader") or ""   # the streamer, not who clipped it
        from forge_routes import save_to_library
        lib = save_to_library(out, f"{safe_name(title)}{' - ' + safe_name(channel) if channel else ''}{out.suffix}")
        return jsonify(ok=True, file=lib, title=title, channel=channel, duration=data.get("duration"),
                       thumbnail=data.get("thumbnail"), size=out.stat().st_size, height=data.get("height"))
    except Exception as e:
        print("Twitch download failed:", e)
        msg = friendly_error(e)
        if "404" in msg or "does not exist" in msg.lower() or "unable to download" in msg.lower():
            msg = "Couldn't find that clip. It may have been deleted, or the link is incomplete."
        return jsonify(error=msg), 502
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/api/clip")
def clip():
    body = request.json or {}
    url = body.get("url", "").strip()
    quality = body.get("quality", "720")
    if not url:
        return jsonify(error="Please paste a YouTube or Twitch link."), 400
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
        # Fastest path: download only the bytes that cover the clip, then cut it locally.
        t0 = time.time()
        try:
            out = fast_clip(url, quality, start, end, workdir)
        except Exception as e:
            print("Fast clip failed, trying the slower ways:", str(e)[-300:])
            out = None
        if out is not None:
            print(f"Fast clip done in {time.time() - t0:.1f}s")
        # Next: let yt-dlp/ffmpeg fetch the part. YouTube sometimes stalls this
        # (often on restricted videos), so it runs with a time limit.
        if out is None and not meta["restricted"]:
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
    if platform(url) == "twitch" and twitch_id(url):
        return "tw_" + twitch_id(url)
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", url)
    return m.group(1) if m else re.sub(r"\W", "_", url)[-40:]


_info_cache: dict = {}  # video id -> (when, yt-dlp info); stream links stay valid for a few hours
INFO_TTL = 3600


def video_info(url: str, fresh: bool = False) -> dict:
    """yt-dlp's info for a video, reused for an hour so each clip skips the 3-10s lookup."""
    vid = video_id(url)
    hit = _info_cache.get(vid)
    if hit and not fresh and time.time() - hit[0] < INFO_TTL:
        return hit[1]
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        data = ydl.extract_info(url, download=False)
    _info_cache[vid] = (time.time(), data)
    return data


# ---------------------------------------------------- fast partial clips ----
FAST_FORMATS = {**QUALITY_FORMATS, "audio": "ba[ext=m4a]/ba/b"}  # m4a audio can be fetched in parts
_encoder: list | None = None


def video_encoder() -> list[str]:
    """NVIDIA GPU encoding when it works on this PC (several times faster), else fast CPU x264."""
    global _encoder
    if _encoder is None:
        ensure_ffmpeg()  # PCs without ffmpeg: wait for the private copy before using it
        _encoder =["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "color=s=1280x720", "-frames:v", "5", "-c:v", "h264_nvenc", "-f", "null", "-"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                _encoder = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "22", "-b:v", "0"]
        except Exception:
            pass
    return _encoder


def _fetch(f: dict, a: int, b: int) -> bytes:
    """Bytes a..b of a stream. YouTube slows down long requests, so fetch in 2 MB pieces."""
    headers = dict(f.get("http_headers") or {})
    out = bytearray()
    while a + len(out) <= b:
        lo = a + len(out)
        headers["Range"] = f"bytes={lo}-{min(b, lo + (2 << 20) - 1)}"
        with urllib.request.urlopen(urllib.request.Request(f["url"], headers=headers), timeout=30) as r:
            piece = r.read()
        if not piece:
            raise RuntimeError("stream ended early")
        out += piece
    return bytes(out)


def _boxes(data: bytes):
    pos = 0
    while pos + 8 <= len(data):
        size, typ = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
        yield typ, pos, size, hdr
        if size < 8:
            return
        pos += size


def dash_part(f: dict, t0: float, t1: float, path: Path) -> bool:
    """Save just the pieces of a fragmented MP4 stream (YouTube DASH) that cover t0..t1.
    The file keeps the original timestamps, so it is cut later by absolute time."""
    head = _fetch(f, 0, 65535)
    sidx = next(((pos, size, hdr) for typ, pos, size, hdr in _boxes(head) if typ == b"sidx"), None)
    if not sidx or sidx[0] + sidx[1] > len(head):
        return False  # not a fragmented stream with an index
    pos, size, hdr = sidx
    b = head[pos + hdr:pos + size]
    timescale = struct.unpack(">I", b[8:12])[0]
    if b[0] == 0:
        ept, first = struct.unpack(">II", b[12:20]); p = 20
    else:
        ept, first = struct.unpack(">QQ", b[12:28]); p = 28
    count = struct.unpack(">H", b[p + 2:p + 4])[0]; p += 4
    offset, t, use = pos + size + first, ept / timescale, []
    for _ in range(count):
        ref, dur, _sap = struct.unpack(">III", b[p:p + 12]); p += 12
        seg_end = t + dur / timescale
        if seg_end > t0 and t < t1:
            use.append((offset, offset + (ref & 0x7FFFFFFF) - 1))
        t, offset = seg_end, offset + (ref & 0x7FFFFFFF)
    if not use:
        return False
    path.write_bytes(head[:pos] + _fetch(f, use[0][0], use[-1][1]))  # header + covering fragments
    return True


def fast_clip(url, quality, start, end, workdir: Path):
    """Download only the part of the video that covers the clip. Returns None if the stream type isn't supported."""
    formats = video_info(url).get("formats") or []
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        selector = ydl.build_format_selector(FAST_FORMATS[quality])
    chosen = next(iter(selector({
        "formats": copy.deepcopy(formats),
        "has_merged_format": any("none" not in (f.get("acodec"), f.get("vcodec")) for f in formats),
        "incomplete_formats": all(f.get("vcodec") == "none" for f in formats)
                              or all(f.get("acodec") == "none" for f in formats),
    })), None)
    if not chosen:
        return None
    fmts = chosen.get("requested_formats") or [chosen]
    audio_only = quality == "audio"
    out = workdir / ("clip.mp3" if audio_only else "clip.mp4")
    acodec = ["-ac", "2", *(["-c:a", "libmp3lame", "-b:a", "192k"] if audio_only else ["-c:a", "aac", "-b:a", "160k"])]

    def encode(cmd_in: list, maps: list, timeout: float):
        x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        encoders = [[]] if audio_only else [video_encoder()] + ([x264] if video_encoder() != x264 else [])
        for enc in encoders:  # if the GPU encoder fails on this clip, redo it on the CPU
            vcodec = [] if audio_only else [*enc, "-pix_fmt", "yuv420p"]
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *cmd_in, *maps, *vcodec, *acodec,
                   *([] if audio_only else ["-movflags", "+faststart"]), str(out)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                return out
            print("Fast clip encode failed:", r.stderr[-300:])
        return None

    # YouTube: fragmented MP4 video/audio streams with an index -> fetch only the needed bytes
    if all((f.get("protocol") or "").startswith("http") and f.get("ext") in ("mp4", "m4a") for f in fmts):
        parts = [workdir / f"part{i}.mp4" for i in range(len(fmts))]
        with ThreadPoolExecutor(len(fmts)) as ex:
            ok = list(ex.map(lambda i: dash_part(fmts[i], start, end, parts[i]), range(len(fmts))))
        if not all(ok):
            return None
        vi = next((i for i, f in enumerate(fmts) if f.get("vcodec") not in (None, "none")), None)
        ai = next((i for i, f in enumerate(fmts) if f.get("acodec") not in (None, "none")), None)
        if audio_only:
            vi = None
        if ai is None and vi is None:
            return None
        cmd_in, graph, maps = [], [], []
        for p in parts:
            cmd_in += ["-copyts", "-i", str(p)]
        if vi is not None:
            graph.append(f"[{vi}:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,format=yuv420p[v]")
            maps += ["-map", "[v]"]
        if ai is not None:
            graph.append(f"[{ai}:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a]")
            maps += ["-map", "[a]"]
        return encode(cmd_in, ["-filter_complex", ";".join(graph), *maps], timeout=120 + (end - start))

    # Twitch VODs: HLS playlists -> ffmpeg jumps straight to the right segments
    if len(fmts) == 1 and "m3u8" in (fmts[0].get("protocol") or ""):
        f = fmts[0]
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        cmd_in = ["-headers", hdrs, "-ss", f"{start}", "-i", f["url"], "-t", f"{end - start}"]
        maps = ["-map", "0:a:0?"] if audio_only else ["-map", "0:v:0?", "-map", "0:a:0?"]
        try:
            return encode(cmd_in, maps, timeout=min(300, 60 + 3 * (end - start)))
        except subprocess.TimeoutExpired:
            print("Fast HLS clip stalled; using the slower ways.")
            return None
    return None


def video_meta(url: str) -> dict:
    vid = video_id(url)
    if vid not in _meta_cache:
        data = video_info(url)
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
        codec = [*video_encoder(), "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start}", "-i", str(src),
           "-t", f"{end - start}", *codec, str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("ffmpeg failed to cut the clip: " + r.stderr[-300:])
    return out


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
