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
    return send_file(library_path(name), conditional=True)


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
    s = pipeline.Settings(
        layout="split" if o.get("layout") == "split" else "full",
        split_ratio=float(o.get("split_ratio") or 0.42),
        face_box=box(o.get("face_box")),
        game_box=box(o.get("game_box")),
        aspect=o.get("aspect", "9:16") if o.get("aspect") in pipeline.ASPECTS else "9:16",
        captions=bool(o.get("captions", True)),
        caption_position=o.get("caption_position", "middle"),
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
                                    api_key=load_config().get("anthropic_api_key") or None)
                st.update(output=os.path.basename(out), result=res, stage="Done", progress=1)
            except Exception as e:
                st["error"] = str(e)
                job.log("ERROR: " + str(e))
                traceback.print_exc()
            finally:
                st.update(done=True, running=False)

    threading.Thread(target=work, daemon=True).start()
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
    return jsonify({k: st.get(k) for k in ("stage", "progress", "log", "done", "error", "output", "result", "running")})


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


@bp.get("/api/config")
def get_config():
    c = load_config()
    return jsonify(has_key=bool(c.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")))


@bp.post("/api/config")
def set_config():
    c = load_config()
    key = (request.get_json(force=True).get("anthropic_api_key") or "").strip()
    if key: c["anthropic_api_key"] = key
    else: c.pop("anthropic_api_key", None)
    with open(CONFIG, "w", encoding="utf-8") as f: json.dump(c, f)
    return jsonify(ok=True, has_key=bool(key))
