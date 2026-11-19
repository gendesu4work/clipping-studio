"""ClipForge routes + the shared clip library (clips saved by the downloader tab)."""
import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from urllib.parse import unquote

from flask import Blueprint, jsonify, request, send_file, abort

import pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(HERE, "jobs")
LIBRARY = os.path.join(HERE, "library")
OUTPUTS = os.path.join(HERE, "outputs")      # finished ClipForge edits, kept across restarts
CONFIG = os.path.join(HERE, "config.json")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
os.makedirs(JOBS, exist_ok=True)
os.makedirs(LIBRARY, exist_ok=True)

bp = Blueprint("forge", __name__)
state = {}            # job id -> dict
render_lock = threading.Lock()


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}


def job_dir(jid):
    if not re.fullmatch(r"[0-9a-f]{12}", jid or ""): abort(404)
    return os.path.join(JOBS, jid)


def library_path(name):
    name = os.path.basename(name or "")
    p = os.path.join(LIBRARY, name)
    if not name or not os.path.isfile(p): abort(404)
    return p


def save_to_library(src_path, name):
    """Called by the downloader after each clip. Returns the stored file name."""
    base, ext = os.path.splitext(os.path.basename(name))
    final, n = base + ext, 2
    while os.path.exists(os.path.join(LIBRARY, final)):
        final = f"{base} ({n}){ext}"; n += 1
    shutil.copyfile(src_path, os.path.join(LIBRARY, final))
    return final


class WebJob(pipeline.Job):
    def __init__(self, jid): self.jid = jid
    def log(self, msg):
        state[self.jid]["log"].append(time.strftime("%H:%M:%S ") + msg)
    def stage(self, name, frac=0.0):
        st = state[self.jid]; st["stage"] = name; st["progress"] = round(float(frac), 3)


def new_job_state(jid):
    return state.setdefault(jid, dict(stage="Ready", progress=0, log=[], done=False, error=None, running=False))


# ------------------------------------------------------------- library
@bp.get("/api/library")
def library_list():
    items = []
    for fn in os.listdir(LIBRARY):
        p = os.path.join(LIBRARY, fn)
        if os.path.isfile(p) and fn.lower().endswith(VIDEO_EXT):
            st = os.stat(p)
            items.append(dict(name=fn, size=st.st_size, mtime=st.st_mtime))
    items.sort(key=lambda x: -x["mtime"])
    return jsonify(items=items)


@bp.get("/api/library/file/<path:name>")
def library_file(name):
    return send_file(library_path(name), conditional=True, as_attachment=request.args.get("dl") == "1")


@bp.delete("/api/library/file/<path:name>")
def library_delete(name):
    os.remove(library_path(name))
    return jsonify(ok=True)


@bp.post("/api/forge/from-library")
def from_library():
    name = (request.get_json(force=True) or {}).get("name")
    src = library_path(name)
    jid = uuid.uuid4().hex[:12]
    d = job_dir(jid); os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "input" + os.path.splitext(src)[1].lower())
    try:
        os.link(src, dst)          # instant, no extra disk space
    except OSError:
        shutil.copyfile(src, dst)
    st = new_job_state(jid)
    st.update(video=dst, video_name=os.path.basename(src))
    try:
        st["info"] = pipeline.probe(dst)
    except Exception as e:
        return jsonify(error=f"Could not read that video: {e}"), 400
    return jsonify(job=jid, name=st["video_name"], info=st["info"])


# ------------------------------------------------------------- my shorts (saved outputs)
def _unique(folder, base, ext):
    final, n = base + ext, 2
    while os.path.exists(os.path.join(folder, final)):
        final = f"{base} ({n}){ext}"; n += 1
    return final


def _put(src, dst):
    try:
        os.link(src, dst)          # same disk: instant, no extra space
    except OSError:
        shutil.copyfile(src, dst)


def save_output(path, source_name, instructions, result, created=None):
    """Keep a finished edit in outputs/ with a readable name + a small .json of details."""
    base = os.path.splitext(source_name or "Short")[0] + " (ClipForge)"
    base = re.sub(r'[\\/:*?"<>|]', "_", base)[:150]
    final = _unique(OUTPUTS, base, ".mp4")
    _put(path, os.path.join(OUTPUTS, final))
    meta = dict(source=source_name or "", instructions=instructions or "",
                created=created or time.time(), **{k: (result or {}).get(k) for k in ("duration", "width", "height", "clips")})
    with open(os.path.join(OUTPUTS, final[:-4] + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return final


def _import_old_jobs():
    """On startup, bring in edits still sitting in jobs/ that My shorts hasn't seen yet
    (made before this tab existed, or by an older server). Each one is imported only once."""
    os.makedirs(OUTPUTS, exist_ok=True)
    seen_file = os.path.join(OUTPUTS, ".imported.json")
    try:
        with open(seen_file, encoding="utf-8") as f: seen = set(json.load(f))
    except Exception:
        seen = set()
    added = False
    for jid in sorted(os.listdir(JOBS)):
        d = os.path.join(JOBS, jid)
        if not os.path.isdir(d): continue
        for fn in sorted(os.listdir(d)):
            m = re.fullmatch(r"edit_(\d+)\.mp4", fn)
            key = f"{jid}/{fn}"
            if not m or key in seen: continue
            try:
                p = os.path.join(d, fn)
                info = pipeline.probe(p)
                stamp = time.strftime("%Y-%m-%d %H-%M", time.localtime(int(m.group(1))))
                save_output(p, f"Short {stamp}.mp4", "", dict(duration=info.get("dur"), width=info.get("w"),
                            height=info.get("h")), created=os.path.getmtime(p))
            except Exception as e:
                print("Could not import old edit", key, e)
            seen.add(key); added = True
    if added:
        with open(seen_file, "w", encoding="utf-8") as f: json.dump(sorted(seen), f)


def _mark_imported(path):
    seen_file = os.path.join(OUTPUTS, ".imported.json")
    try:
        with open(seen_file, encoding="utf-8") as f: seen = json.load(f)
    except Exception:
        seen = []
    seen.append(os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path))
    with open(seen_file, "w", encoding="utf-8") as f: json.dump(sorted(set(seen)), f)


_import_old_jobs()


def output_path(name):
    name = os.path.basename(name or "")
    p = os.path.join(OUTPUTS, name)
    if not name.lower().endswith(".mp4") or not os.path.isfile(p): abort(404)
    return p


@bp.get("/api/outputs")
def outputs_list():
    items = []
    for fn in os.listdir(OUTPUTS):
        p = os.path.join(OUTPUTS, fn)
        if not (fn.lower().endswith(".mp4") and os.path.isfile(p)): continue
        meta = {}
        try:
            with open(p[:-4] + ".json", encoding="utf-8") as f: meta = json.load(f)
        except Exception:
            pass
        items.append(dict(meta, name=fn, size=os.path.getsize(p), created=meta.get("created") or os.path.getmtime(p)))
    items.sort(key=lambda x: -x["created"])
    return jsonify(items=items)


@bp.get("/api/outputs/file/<path:name>")
def outputs_file(name):
    return send_file(output_path(name), conditional=True, mimetype="video/mp4",
                     as_attachment=request.args.get("dl") == "1")


@bp.delete("/api/outputs/file/<path:name>")
def outputs_delete(name):
    p = output_path(name)
    os.remove(p)
    if os.path.exists(p[:-4] + ".json"): os.remove(p[:-4] + ".json")
    return jsonify(ok=True)


# ------------------------------------------------------------- forge
@bp.post("/api/upload")
def upload():
    """Raw-body upload (no multipart): ?kind=video|music&job=<id>, header X-Filename."""
    kind = request.args.get("kind", "video")
    name = os.path.basename(unquote(request.headers.get("X-Filename", "file")))
    ext = os.path.splitext(name)[1].lower()[:6] or (".mp4" if kind == "video" else ".mp3")
    jid = request.args.get("job") or uuid.uuid4().hex[:12]
    d = job_dir(jid); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, ("input" if kind == "video" else "music") + ext)
    with open(path, "wb") as f:
        while True:
            chunk = request.stream.read(1 << 20)
            if not chunk: break
            f.write(chunk)
    st = new_job_state(jid)
    st[kind] = path
    st[kind + "_name"] = name
    if kind == "video":
        try:
            st["info"] = pipeline.probe(path)
        except Exception as e:
            return jsonify(error=f"Could not read that video: {e}"), 400
    return jsonify(job=jid, name=name, info=st.get("info"))


@bp.post("/api/start")
def start():
    body = request.get_json(force=True)
    jid = body.get("job"); d = job_dir(jid)
    st = state.get(jid)
    if not st or "video" not in st: return jsonify(error="Choose a video first."), 400
    if st.get("running"): return jsonify(error="Already editing."), 409
    o = body.get("options", {})
    box = lambda b: [float(v) for v in b][:4] if isinstance(b, list) and len(b) == 4 else None
    focus = []
    for f in (o.get("focus") or [])[:20]:
        try:
            a = None if f.get("start") in (None, "") else float(f["start"])
            b = None if f.get("end") in (None, "") else float(f["end"])
        except (TypeError, ValueError, AttributeError):
            continue
        if a is not None and b is not None and b < a: a, b = b, a
        if a is not None and (b is None or b - a < 0.3): b = a + 3.0     # a single mark = the 3s after it
        note = str(f.get("note") or "").strip()[:200]
        if a is not None or note: focus.append(dict(start=a, end=b if a is not None else None, note=note))
    s = pipeline.Settings(
        layout=o.get("layout") if o.get("layout") in ("split", "framed") else "full",
        heavy=bool(o.get("heavy", False)),
        title=str(o.get("title") or "").strip()[:120],
        handle=str(o.get("handle") or "").strip()[:40],
        focus=focus,
        split_ratio=float(o.get("split_ratio") or 0.42),
        face_box=box(o.get("face_box")),
        game_box=box(o.get("game_box")),
        aspect=o.get("aspect", "9:16") if o.get("aspect") in pipeline.ASPECTS else "9:16",
        captions=bool(o.get("captions", True)),
        caption_position=o.get("caption_position", "middle"),
        caption_font=str(o.get("caption_font") or "arialbd.ttf"),
        vignette=bool(o.get("vignette", True)),
        music=bool(o.get("music", True)),
        max_pause={"relaxed": 0.6, "normal": 0.35, "tight": 0.2}.get(o.get("pace"), 0.35),
    )
    st.update(stage="Starting", progress=0, log=[], done=False, error=None, running=True, output=None)
    out = os.path.join(d, f"edit_{int(time.time())}.mp4")
    instructions = body.get("instructions", "")

    def work():
        job = WebJob(jid)
        with render_lock:
            try:
                res = pipeline.edit(st["video"], out, instructions, s, st.get("music"), job,
                                    claude=claude_settings())
                st.update(output=os.path.basename(out), result=res, stage="Done", progress=1)
                try:
                    st["saved_as"] = save_output(out, st.get("video_name"), instructions, res)
                    _mark_imported(out)
                except Exception as e:
                    job.log(f"Could not save to My shorts: {e}")
            except Exception as e:
                st["error"] = str(e)
                job.log("ERROR: " + str(e))
                traceback.print_exc()
            finally:
                st.update(done=True, running=False)

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


# ---------- caption fonts ----------
def _font(fid):
    return next((f for f in pipeline.font_catalog() if f["id"] == fid), None) or abort(404)


@bp.get("/api/fonts")
def fonts_list():
    return jsonify(fonts=[{k: f[k] for k in ("id", "name", "custom")} for f in pipeline.font_catalog()])


@bp.get("/api/fonts/file/<path:fid>")
def fonts_file(fid):
    return send_file(_font(fid)["path"], max_age=86400)


@bp.post("/api/fonts")
def fonts_add():
    f = request.files.get("file")
    if not f or not f.filename: return jsonify(error="Choose a font file."), 400
    name = re.sub(r"[^\w.\- ()]", "_", os.path.basename(f.filename)).strip() or "font.ttf"
    if not name.lower().endswith(pipeline.FONT_EXT):
        return jsonify(error="Fonts must be .ttf or .otf files."), 400
    os.makedirs(pipeline.USER_FONTS, exist_ok=True)
    base, ext = os.path.splitext(name); final, n = name, 2
    while os.path.exists(os.path.join(pipeline.USER_FONTS, final)):
        final = f"{base} ({n}){ext}"; n += 1
    dest = os.path.join(pipeline.USER_FONTS, final)
    f.save(dest)
    try:
        pipeline.font_info(dest)
    except Exception:
        os.remove(dest)
        return jsonify(error="That file isn't a readable font."), 400
    return jsonify(font=next({k: x[k] for k in ("id", "name", "custom")}
                             for x in pipeline.font_catalog() if x["id"] == "user:" + final))


@bp.delete("/api/fonts/<path:fid>")
def fonts_remove(fid):
    f = _font(fid)
    if not f["custom"]: return jsonify(error="Built-in fonts can't be removed."), 400
    os.remove(f["path"])
    return jsonify(ok=True)


@bp.get("/api/forge/frame/<jid>")
def frame(jid):
    """A JPEG of the source video at ?t=seconds (for the split-layout box editor)."""
    import cv2
    job_dir(jid)
    st = state.get(jid) or abort(404)
    cap = cv2.VideoCapture(st["video"])
    t = float(request.args.get("t", 0) or 0)
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
    ok, f = cap.read(); cap.release()
    if not ok: abort(404)
    if f.shape[1] > 1280:
        f = cv2.resize(f, (1280, int(f.shape[0] * 1280 / f.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes(), 200, {"Content-Type": "image/jpeg", "Cache-Control": "no-store"}


@bp.get("/api/forge/autobox/<jid>")
def autobox(jid):
    """Auto-detected facecam / gameplay boxes for the split layout (normalized 0-1)."""
    job_dir(jid)
    st = state.get(jid) or abort(404)
    ratio = float(request.args.get("split", 0.42))
    key = f"autobox_{ratio:.3f}"
    if key not in st:
        st[key] = pipeline.auto_boxes(st["video"], st["info"], ratio)
    return jsonify(st[key])


@bp.get("/api/status/<jid>")
def status(jid):
    job_dir(jid)
    st = state.get(jid) or abort(404)
    return jsonify({k: st.get(k) for k in ("stage", "progress", "log", "done", "error", "output", "result", "running", "saved_as")})


@bp.get("/api/file/<jid>/<name>")
def file(jid, name):
    d = job_dir(jid)
    if not re.fullmatch(r"(edit_\d+\.mp4|input\.\w+)", name): abort(404)
    p = os.path.join(d, name)
    if not os.path.exists(p): abort(404)
    st = state.get(jid, {})
    nice = os.path.splitext(st.get("video_name") or "video")[0] + " (ClipForge).mp4"
    return send_file(p, conditional=True, download_name=nice,
                     as_attachment=request.args.get("dl") == "1")


def claude_settings():
    c = load_config()
    return {"api_key": c.get("anthropic_api_key") or None,
            "base_url": c.get("anthropic_base_url") or None,
            "model": c.get("anthropic_model") or None}


@bp.get("/api/config")
def get_config():
    c = load_config()
    return jsonify(has_key=bool(c.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")),
                   base_url=c.get("anthropic_base_url", ""), model=c.get("anthropic_model", ""),
                   free=bool(c.get("anthropic_api_key")) and "coarena" in c.get("anthropic_base_url", ""))


@bp.post("/api/config")
def set_config():
    from director import check_key
    c = load_config()
    body = request.get_json(force=True)
    if body.get("clear"):
        for k in ("anthropic_api_key", "anthropic_base_url", "anthropic_model"): c.pop(k, None)
    else:
        key = (body.get("anthropic_api_key") or "").strip() or c.get("anthropic_api_key", "")
        base = (body.get("base_url") or "").strip().rstrip("/")
        model = (body.get("model") or "").strip()
        if base and not base.startswith(("http://", "https://")): base = "https://" + base
        if not key:
            return jsonify(error="Paste an API key first."), 400
        err = check_key({"api_key": key, "base_url": base or None, "model": model or None})
        if err:
            return jsonify(error=err), 400
        c["anthropic_api_key"] = key
        for k, v in (("anthropic_base_url", base), ("anthropic_model", model)):
            if v: c[k] = v
            else: c.pop(k, None)
    with open(CONFIG, "w", encoding="utf-8") as f: json.dump(c, f)
    return jsonify(ok=True, has_key=bool(c.get("anthropic_api_key")))
