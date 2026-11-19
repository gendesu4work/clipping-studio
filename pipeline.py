"""ClipForge editing engine.

Turns a talking-head / interview video into a short:
  transcribe -> plan (director) -> cut dead air -> face-follow reframe
  -> word-by-word captions -> vignette -> music bed -> loudness normalize.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FACE_MODEL = os.path.join(HERE, "models", "face_detection_yunet_2023mar.onnx")

ASPECTS = {"9:16": (1080, 1920), "3:4": (1080, 1440), "4:5": (1080, 1350), "1:1": (1080, 1080)}
COLORS = {  # RGB
    "yellow": (255, 230, 0), "red": (255, 59, 59), "green": (61, 255, 110),
    "magenta": (255, 79, 216), "orange": (255, 159, 28), "cyan": (40, 220, 255),
}


@dataclass
class Settings:
    aspect: str = "9:16"
    captions: bool = True
    caption_position: str = "middle"   # top | middle | bottom
    caption_font: str = "arialbd.ttf"  # id from font_catalog(): a Windows font file or one added to fonts/
    vignette: bool = True
    music: bool = True                 # only used if a music file is supplied
    music_level_db: float = -12.0      # music bed relative to the voice
    max_pause: float = 0.35            # gaps longer than this get cut
    pad_start: float = 0.06
    pad_end: float = 0.12
    words_per_page: int = 6
    whisper_model: str = "small"
    # layout: "full" = one face-following crop; "split" = facecam on top, gameplay below (always 9:16)
    layout: str = "full"
    split_ratio: float = 0.42          # share of the height used by the facecam panel
    face_box: list | None = None       # [x, y, w, h] normalized 0-1 in the source; None = auto
    game_box: list | None = None


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Plan:
    keep: list = field(default_factory=list)        # [(from_idx, to_idx)] inclusive
    keep_times: list = field(default_factory=list)  # [(start_s, end_s)] - when set, used instead of keep
    highlights: dict = field(default_factory=dict)  # word idx -> color name
    summary: str = ""
    hook: dict | None = None                        # {"text", "glow_words", "color", "seconds"} or None
    unsupported: list = field(default_factory=list)  # requests the editor couldn't carry out


class Job:
    """Progress sink. The server replaces these callbacks."""
    def log(self, msg: str): print(msg, flush=True)
    def stage(self, name: str, frac: float = 0.0): print(f"[{name}] {frac:.0%}", flush=True)


# ---------------------------------------------------------------- helpers
def run(cmd, **kw):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        if cmd[0] not in ("ffmpeg", "ffprobe"):
            raise
        from ffmpeg_tools import ensure_ffmpeg
        if not ensure_ffmpeg():  # first run on a PC without ffmpeg: fetch it, then retry
            raise RuntimeError("FFmpeg isn't installed and couldn't be downloaded automatically. "
                               "Check your internet connection and restart Creator Studio.") from None
        r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{r.stderr[-2000:]}")
    return r.stdout


def probe(path):
    out = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path]))
    v = next(s for s in out["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in out["streams"])
    num, den = v["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd: rot = int(sd["rotation"])
    return dict(w=int(v["width"]), h=int(v["height"]), fps=fps, dur=float(out["format"]["duration"]),
                has_audio=has_audio, rotation=rot)


# ---------------------------------------------------------------- 1. transcribe
def transcribe(src, job: Job, settings: Settings, dur: float):
    from faster_whisper import WhisperModel
    job.stage("Transcribing", 0)
    job.log(f"Loading speech model '{settings.whisper_model}'...")
    model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    segs, info = model.transcribe(src, word_timestamps=True, vad_filter=False, beam_size=5)
    words = []
    for seg in segs:
        for w in seg.words or []:
            t = w.word.strip()
            if t: words.append(Word(t, float(w.start), float(w.end)))
        job.stage("Transcribing", min(1.0, seg.end / max(dur, 1e-3)))
    job.log(f"Transcribed {len(words)} words (language: {info.language}).")
    return words


# ---------------------------------------------------------------- 2. cut list
def build_segments(words, plan: Plan, s: Settings, dur: float):
    """Kept words -> list of (start, end, [word indices]) with dead air removed.

    A time-based plan (from a model that watched the video) is kept as-is, silent action included."""
    if plan.keep_times:
        out = []
        for a, b in plan.keep_times:
            idx = [i for i, w in enumerate(words) if a <= (w.start + w.end) / 2 < b]
            out.append((a, b, idx))
        return out
    kept = sorted({i for a, b in plan.keep for i in range(a, b + 1) if 0 <= i < len(words)})
    segs = []
    for i in kept:
        w = words[i]
        if segs and i - 1 in segs[-1][2] and w.start - words[i - 1].end <= s.max_pause:
            segs[-1][2].append(i)
        else:
            segs.append([None, None, [i]])
    out = []
    for _, _, idx in segs:
        a = max(0.0, words[idx[0]].start - s.pad_start)
        b = min(dur, words[idx[-1]].end + s.pad_end)
        if out and a <= out[-1][1]:            # overlapping pads -> merge
            out[-1] = (out[-1][0], b, out[-1][2] + idx)
        else:
            out.append((a, b, idx))
    return out


# ---------------------------------------------------------------- 3. face-follow reframe
def _crop_geometry(w, h, aspect):
    ow, oh = ASPECTS[aspect]
    target = ow / oh
    if w / h > target:      # source wider -> full height, pan horizontally
        return dict(axis="x", cw=h * target, ch=float(h), ow=ow, oh=oh)
    return dict(axis="y", cw=float(w), ch=w / target, ow=ow, oh=oh)


def analyze(src, info, frame_ranges, job: Job, roi=None):
    """Pass 1: face centers + scene-cut flags for every kept frame.
    roi=(x0, y0, x1, y1) in source px limits face detection to that region
    (used for the facecam in split layout)."""
    want = np.zeros(int(info["dur"] * info["fps"]) + 10, bool)
    for n0, n1 in frame_ranges: want[n0:n1] = True
    det_step = max(1, round(info["fps"] / 15))
    scale = 640 / max(info["w"], info["h"])
    dw, dh = int(info["w"] * scale), int(info["h"] * scale)
    if roi:
        rx0, ry0, rx1, ry1 = [int(round(v)) for v in roi]
        rs = min(4.0, 640 / max(rx1 - rx0, ry1 - ry0))
        rsize = (max(1, int((rx1 - rx0) * rs)), max(1, int((ry1 - ry0) * rs)))
        det = cv2.FaceDetectorYN.create(FACE_MODEL, "", rsize, 0.55, 0.3, 20)
    else:
        det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (dw, dh), 0.6, 0.3, 50)
    cap = cv2.VideoCapture(src)
    faces, cuts, prev_hist = {}, set(), None
    i, total = 0, int(want.sum())
    done = 0
    while True:
        if i >= len(want): break
        if not want[i] and not (i + 1 < len(want) and want[i + 1]):
            if not cap.grab(): break
            i += 1; prev_hist = None; continue
        ok, f = cap.read()
        if not ok: break
        small = cv2.resize(f, (dw, dh), interpolation=cv2.INTER_AREA)
        # camera-switch detection: in split mode only watch the facecam (gameplay motion isn't a cut)
        watch = f[ry0:ry1, rx0:rx1] if roi else small
        thumb = cv2.cvtColor(cv2.resize(watch, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev_hist is not None and want[i] and float(np.mean(np.abs(thumb - prev_hist))) > 12:
            cuts.add(i)
        prev_hist = thumb
        if want[i]:
            if i % det_step == 0:
                if roi:
                    crop = cv2.resize(f[ry0:ry1, rx0:rx1], rsize, interpolation=cv2.INTER_AREA)
                    _, fs = det.detect(crop)
                    if fs is not None and len(fs):
                        x, y, fw, fh = max(fs, key=lambda r: r[2] * r[3] * r[-1])[:4]
                        faces[i] = (rx0 + (x + fw / 2) / rs, ry0 + (y + fh * 0.45) / rs, fh / rs)
                else:
                    _, fs = det.detect(small)
                    if fs is not None and len(fs):
                        # biggest confident face
                        x, y, fw, fh = max(fs, key=lambda r: r[2] * r[3] * r[-1])[:4]
                        faces[i] = ((x + fw / 2) / scale, (y + fh * 0.45) / scale, fh / scale)
            done += 1
            if done % 30 == 0: job.stage("Tracking faces", done / max(total, 1))
        i += 1
    cap.release()
    job.log(f"Face found on {len(faces)} sampled frames, {len(cuts)} camera switches detected.")
    return faces, cuts


def camera_path(frames, faces, cuts, geo, info):
    """Full-frame layout: pan along one axis to follow the face."""
    span = geo["cw"] if geo["axis"] == "x" else geo["ch"]
    full = info["w"] if geo["axis"] == "x" else info["h"]
    k = 0 if geo["axis"] == "x" else 1
    default = full / 2 if geo["axis"] == "x" else full * 0.4
    return smooth_track(frames, faces, cuts, k, span, span / 2, full - span / 2, default, info["fps"])


def smooth_track(frames, faces, cuts, k, span, lo, hi, default, fps):
    """Virtual camera on one axis: hold inside a deadzone, glide with a critically
    damped spring when the subject leaves it, snap on camera switches/edit points."""
    if hi < lo: lo = hi = (lo + hi) / 2
    # split into shots
    shots, cur = [], []
    for j, n in enumerate(frames):
        if cur and (n != frames[j - 1] + 1 or n in cuts):
            shots.append(cur); cur = []
        cur.append(n)
    if cur: shots.append(cur)
    pos = {}
    omega, dead = 7.0, 0.07 * span          # spring stiffness, deadzone half-width
    look = int(0.12 * fps)                   # small look-ahead hides spring lag
    for shot in shots:
        raw = np.array([faces[n][k] if n in faces else np.nan for n in shot], float)
        if np.all(np.isnan(raw)):
            raw[:] = default
        else:
            idx = np.arange(len(raw)); m = ~np.isnan(raw)
            raw = np.interp(idx, idx[m], raw[m])
        win = max(1, int(0.25 * fps)) | 1    # median kills detector jitter
        pad = np.pad(raw, win // 2, mode="edge")
        tgt = np.array([np.median(pad[t:t + win]) for t in range(len(raw))])
        tgt = np.concatenate([tgt[look:], np.repeat(tgt[-1], min(look, len(tgt)))])[:len(raw)]
        # deadzone hold
        desired = np.empty_like(tgt); hold = tgt[0]
        for t, v in enumerate(tgt):
            if abs(v - hold) > dead: hold = v
            desired[t] = hold
        # spring
        x, vel, dt = desired[0], 0.0, 1.0 / fps
        for t, n in enumerate(shot):
            acc = omega * omega * (desired[t] - x) - 2 * omega * vel
            vel += acc * dt; x += vel * dt
            pos[n] = float(np.clip(x, lo, hi))
    return pos


# ---------------------------------------------------------------- 3b. split layout (facecam + gameplay)
def split_dims(split_ratio, ow=1080, oh=1920):
    top_h = int(round(oh * min(0.6, max(0.25, split_ratio)) / 2)) * 2
    return ow, top_h, oh - top_h


def fit_aspect(box, aspect, w, h):
    """Largest box with the given w/h aspect centred inside `box` (x, y, bw, bh in px), clamped to the frame."""
    x, y, bw, bh = box
    if bw / max(bh, 1) > aspect: nw, nh = bh * aspect, bh
    else: nw, nh = bw, bw / aspect
    nw, nh = min(nw, w, h * aspect), min(nh, h, w / aspect)
    cx, cy = x + bw / 2, y + bh / 2
    nx = min(max(cx - nw / 2, 0), w - nw); ny = min(max(cy - nh / 2, 0), h - nh)
    return [nx, ny, nw, nh]


def auto_boxes(src, info, split_ratio=0.42, samples=24):
    """Guess where the facecam and the gameplay are. Returns normalized boxes.
    The facecam face is the one that stays in the same place across the video;
    faces in the gameplay move around, so we take the most frequent position."""
    w, h = info["w"], info["h"]
    ow, top_h, bot_h = split_dims(split_ratio)
    a_top, a_bot = ow / top_h, ow / bot_h
    scale = min(1.0, 1280 / max(w, h))
    dw, dh = int(w * scale), int(h * scale)
    det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (dw, dh), 0.6, 0.3, 50)
    cap = cv2.VideoCapture(src)
    hits, grays = [], []
    for k in range(samples):
        cap.set(cv2.CAP_PROP_POS_MSEC, (k + 0.5) / samples * info["dur"] * 1000)
        ok, f = cap.read()
        if not ok: continue
        small = cv2.resize(f, (dw, dh), interpolation=cv2.INTER_AREA)
        grays.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        _, fs = det.detect(small)
        for r in (fs if fs is not None else []):
            x, y, fw, fh = r[:4] / scale
            hits.append((x + fw / 2, y + fh / 2, fh, float(r[-1])))
    cap.release()
    found = bool(hits)
    overlay = None
    if hits:
        # vote on a coarse grid, then take the median inside the winning cell
        cell = lambda p: (int(p[0] / (w / 8)), int(p[1] / (h / 6)))
        votes = {}
        for p in hits: votes.setdefault(cell(p), []).append(p)
        best = max(votes.values(), key=lambda v: (len(v), sum(p[3] for p in v)))
        cx, cy, fh = (float(np.median([p[i] for p in best])) for i in range(3))
        overlay = find_overlay(grays, cx * scale, cy * scale, fh * scale)
        if overlay is not None:
            ox, oy, ow_, oh_ = [v / scale for v in overlay]
            bh = min(oh_, ow_ / a_top)              # biggest panel-shaped box inside the facecam
            bx = min(max(cx - bh * a_top / 2, ox), ox + ow_ - bh * a_top)
            by = min(max(cy - bh * 0.45, oy), oy + oh_ - bh)
            face = [bx, by, bh * a_top, bh]
        else:
            bh = min(h, max(fh * 3.0, h * 0.2))
            face = fit_aspect([cx - bh * a_top / 2, cy - bh * 0.45, bh * a_top, bh], a_top, w, h)
    else:   # typical streamer layout: facecam bottom-left
        bh = h * 0.4
        face = fit_aspect([0, h - bh, bh * a_top, bh], a_top, w, h)
    # gameplay: full height, centred - but slide sideways so the facecam overlay isn't in it
    game = fit_aspect([0, 0, w, h], a_bot, w, h)
    cam = [v / scale for v in overlay] if overlay is not None else face
    gx, gw = game[0], game[2]
    gap = w * 0.008                                  # clear the overlay's border line too
    cam_l, cam_r = cam[0] - gap, cam[0] + cam[2] + gap
    if gx < cam_r and gx + gw > cam_l:              # overlapping horizontally
        if cam_l + cam[2] / 2 < w / 2 and cam_r + gw <= w: game[0] = cam_r
        elif cam_l - gw >= 0: game[0] = cam_l - gw
    n = lambda b: [b[0] / w, b[1] / h, b[2] / w, b[3] / h]
    return dict(face_box=n(face), game_box=n(game), face_found=found, detections=len(hits),
                overlay=n([v / scale for v in overlay]) if overlay is not None else None)


def find_overlay(grays, cx, cy, fh):
    """Find the facecam overlay rectangle around a face (all values in the downscaled frame).
    Gameplay changes from frame to frame and blurs out in the median, while the overlay's
    border stays put, so we look for the nearest strong straight edge on each side."""
    med = np.median(np.stack(grays), axis=0).astype(np.float32)
    H, W = med.shape
    gx = np.abs(cv2.Sobel(med, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(med, cv2.CV_32F, 0, 1, ksize=3))
    y0, y1 = int(max(0, cy - fh * 1.2)), int(min(H, cy + fh * 1.2))
    x0, x1 = int(max(0, cx - fh * 1.2)), int(min(W, cx + fh * 1.2))
    col = gx[y0:y1].mean(axis=0)      # vertical-edge strength per column (rows around the face)
    row = gy[:, x0:x1].mean(axis=1)   # horizontal-edge strength per row

    def nearest(profile, start, stop, step, limit):
        idx = list(range(int(start), int(stop), step))
        if not idx: return limit
        vals = np.array([profile[i] for i in idx])
        if vals.max() < max(12.0, 4 * np.median(profile)): return limit   # no clear border: frame edge
        thr = 0.6 * vals.max()
        for i, v in zip(idx, vals):
            if v >= thr: return i
        return limit

    far = fh * 7
    L = nearest(col, cx - fh * 0.9, max(0, cx - far) - 1, -1, 0)
    R = nearest(col, cx + fh * 0.9, min(W, cx + far), 1, W)
    T = nearest(row, cy - fh * 0.9, max(0, cy - far) - 1, -1, 0)
    B = nearest(row, cy + fh * 1.1, min(H, cy + far), 1, H)
    m = 3                                   # stay inside the border line itself
    L, T = L + m if L > 0 else 0, T + m if T > 0 else 0
    R, B = R - m if R < W else W, B - m if B < H else H
    if R - L < fh * 1.5 or B - T < fh * 1.5: return None
    return [L, T, R - L, B - T]


def split_setup(src, info, s: Settings):
    w, h = info["w"], info["h"]
    ow, top_h, bot_h = split_dims(s.split_ratio)
    boxes = None
    if not (s.face_box and s.game_box):
        boxes = auto_boxes(src, info, s.split_ratio)
    fb = s.face_box or boxes["face_box"]
    gb = s.game_box or boxes["game_box"]
    px = lambda b: [b[0] * w, b[1] * h, b[2] * w, b[3] * h]
    face = fit_aspect(px(fb), ow / top_h, w, h)
    game = fit_aspect(px(gb), ow / bot_h, w, h)
    return dict(layout="split", ow=ow, oh=1920, top_h=top_h, bot_h=bot_h, face=face, game=game,
                auto=boxes)


# ---------------------------------------------------------------- 4. captions (ASS)
USER_FONTS = os.path.join(HERE, "fonts")          # fonts the creator added
FONT_EXT = (".ttf", ".otf")
SYSTEM_FONTS = ["arialbd.ttf", "ariblk.ttf", "impact.ttf", "seguibl.ttf", "verdanab.ttf",
                "trebucbd.ttf", "tahomabd.ttf", "calibrib.ttf", "georgiab.ttf", "comicbd.ttf"]
_font_cache = {}


def font_info(path):
    """(family, bold, display name) read from the font file's name table."""
    key = (path, os.path.getmtime(path))
    if key not in _font_cache:
        import io
        from fontTools.ttLib import TTFont
        with open(path, "rb") as fh:           # read into memory so Windows never keeps the file locked
            data = fh.read()
        names = TTFont(io.BytesIO(data), fontNumber=0)["name"]
        fam = str(names.getDebugName(1) or os.path.splitext(os.path.basename(path))[0])
        sub = str(names.getDebugName(2) or "Regular")
        bold = "bold" in sub.lower()
        _font_cache[key] = (fam, bold, fam if sub.lower() in ("regular", "normal", "bold") else f"{fam} {sub}")
    return _font_cache[key]


def font_catalog():
    """[{id, name, family, bold, path, custom}] - system caption fonts first, then the creator's own."""
    out, sysdir = [], os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    for fn in SYSTEM_FONTS:
        p = os.path.join(sysdir, fn)
        if os.path.exists(p):
            try:
                fam, bold, name = font_info(p)
            except Exception:   # unreadable font: skip it rather than break the whole list
                continue
            out.append(dict(id=fn, name=name, family=fam, bold=bold, path=p, custom=False))
    if os.path.isdir(USER_FONTS):
        for fn in sorted(os.listdir(USER_FONTS), key=str.lower):
            p = os.path.join(USER_FONTS, fn)
            if fn.lower().endswith(FONT_EXT):
                try:
                    fam, bold, name = font_info(p)
                except Exception:
                    continue
                out.append(dict(id="user:" + fn, name=name, family=fam, bold=bold, path=p, custom=True))
    return out


def resolve_font(font_id):
    cat = font_catalog()
    return next((f for f in cat if f["id"] == font_id), None) or (cat[0] if cat else
            dict(id="", name="Arial", family="Arial", bold=True, path=None, custom=False))


def _ass_color(rgb, alpha=0):
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _bgr(rgb):
    r, g, b = rgb
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def _hook_event(hook, ow, y, fs, total, t):
    """The hook title: big bold text at the top, chosen words glowing in a colour, popping in at the start."""
    hs = round(fs * 1.3)
    max_chars = 16
    glow = {re.sub(r"[^\w]", "", w.lower()) for g in hook["glow_words"] for w in g.split()}
    glow.discard("")
    col = _bgr(COLORS.get(hook["color"], COLORS["yellow"]))
    # Two layers with identical layout: a blurred colour halo behind the glowing words, then crisp text on top.
    halo, text, line_len = [], [], 0
    hidden = r"{\alpha&HFF&}"
    for word in hook["text"].upper().split():
        word = _esc(word)
        if line_len and line_len + 1 + len(word) > max_chars:
            halo.append(r"\N"); text.append(r"\N"); line_len = 0
        elif line_len:
            halo.append(" "); text.append(" "); line_len += 1
        if re.sub(r"[^\w]", "", word.lower()) in glow:
            halo.append("{" + rf"\1a&H60&\1c{col}\3a&H10&\3c{col}\4a&HFF&" + "}" + word + hidden)
            text.append("{" + rf"\1c{col}" + "}" + word + r"{\1c&HFFFFFF&}")
        else:
            halo.append(hidden + word)
            text.append(word)
        line_len += len(word)
    end = total if not hook["seconds"] else min(total, hook["seconds"])
    pop = r"\fscx70\fscy70\t(0,160,\fscx106\fscy106)\t(160,260,\fscx100\fscy100)"
    base = rf"\an8\pos({ow / 2:.0f},{y:.0f})\fad(120,250)\fs{hs}" + pop
    halo_head = "{" + base + rf"\bord{hs * 0.26:.1f}\blur{hs * 0.55:.1f}" + "}"
    text_head = "{" + base + (rf"\1c&HFFFFFF&\3c&H000000&\bord{hs * 0.07:.1f}\shad{hs * 0.05:.1f}"
                              rf"\4a&H60&\blur{hs * 0.04:.1f}") + "}"
    rows = []
    if glow:
        rows.append(f"Dialogue: 1,{t(0)},{t(end)},Cap,,0,0,0,,{halo_head}{''.join(halo)}")
    rows.append(f"Dialogue: 2,{t(0)},{t(end)},Cap,,0,0,0,,{text_head}{''.join(text)}")
    return "\n".join(rows)


def build_ass(words, segments, plan: Plan, s: Settings, geo, path):
    ow, oh = geo["ow"], geo["oh"]
    font = resolve_font(s.caption_font)
    fs = round(oh * 0.032) if ow / oh < 0.6 else round(oh * 0.036)
    if geo.get("layout") == "split":   # "middle" sits on the seam between facecam and gameplay
        th = geo["top_h"]
        y = {"top": th * 0.16, "middle": th, "bottom": oh * 0.86}.get(s.caption_position, th)
    else:
        y = {"top": 0.22, "middle": 0.52, "bottom": 0.78}.get(s.caption_position, 0.52) * oh
    hook_y = oh * 0.10
    if plan.hook and s.caption_position == "top":
        y = max(y, oh * 0.24)   # keep top captions clear of the hook title
    max_chars = 18 if ow / oh < 0.7 else 24
    # map source word times -> output timeline
    timeline, t_out = [], 0.0
    for a, b, idx in segments:
        for i in idx:
            w = words[i]
            timeline.append((i, t_out + max(0, w.start - a), t_out + min(b - a, w.end - a)))
        t_out += b - a
    total = t_out
    # pages
    pages, cur = [], []
    for j, (i, st, en) in enumerate(timeline):
        if cur and (len(cur) >= s.words_per_page or st - cur[-1][2] > 0.6):
            pages.append(cur); cur = []
        cur.append((i, st, en))
        if words[i].text[-1:] in ".?!" and len(cur) >= 2:
            pages.append(cur); cur = []
    if cur: pages.append(cur)

    def t(x):
        x = max(0, x); h = int(x // 3600); m = int(x % 3600 // 60); sec = x % 60
        return f"{h}:{m:02d}:{sec:05.2f}"

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {ow}", f"PlayResY: {oh}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font['family'].replace(',', ' ')},{fs},&H00FFFFFF,&H00FFFFFF,&H90000000,&H80000000,{-1 if font['bold'] else 0},0,0,0,100,100,{fs * 0.16:.1f},0,1,{fs * 0.05:.1f},{fs * 0.07:.1f},5,40,40,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if plan.hook:
        lines.append(_hook_event(plan.hook, ow, hook_y, fs, total, t))
    for p, page in enumerate(pages if s.captions else []):
        start = page[0][1]
        nxt = pages[p + 1][0][1] if p + 1 < len(pages) else page[-1][2] + 0.8
        end = min(nxt, page[-1][2] + 0.8)
        # manual line wrap
        out, line_len = [], 0
        for k, (i, st, en) in enumerate(page):
            word = _esc(words[i].text.upper())
            if line_len and line_len + 1 + len(word) > max_chars:
                out.append("\\N"); line_len = 0
            elif line_len:
                out.append(" "); line_len += 1
            rel = int((st - start) * 1000)
            col = plan.highlights.get(i) if plan.highlights.get(i) in COLORS else None
            fill = _bgr(COLORS[col]) if col else "&HFFFFFF&"
            # word fades in grey -> white (or its highlight colour); fill and glow alphas animate separately
            oa = "90" if col else "70"
            tags = (f"{{\\1a&HFF&\\3a&HFF&\\4a&HFF&\\1c&HC8C8C8&\\t({rel},{rel + 70},\\1a&H55&)"
                    f"\\t({rel + 70},{rel + 230},\\1a&H00&\\3a&H{oa}&\\4a&H80&\\1c{fill})")
            if col:
                tags += f"\\3c{fill}\\bord{fs * 0.09:.1f}\\blur{fs * 0.3:.1f}"
            else:
                tags += f"\\3c&H000000&\\bord{fs * 0.05:.1f}\\blur{fs * 0.08:.1f}"
            out.append(tags + "}" + word)
            line_len += len(word)
        text = f"{{\\pos({ow / 2:.0f},{y:.0f})\\fad(0,90)}}" + "".join(out)
        lines.append(f"Dialogue: 0,{t(start)},{t(end)},Cap,,0,0,0,,{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(pages)


# ---------------------------------------------------------------- 5. vignette
def build_vignette(geo, path, strength=0.62):
    ow, oh = geo["ow"], geo["oh"]
    y = np.linspace(0, 1, oh)[:, None]
    if geo.get("layout") == "split":
        # soft dark band on the seam (where captions sit) + gentle top/bottom edges
        seam = geo["top_h"] / oh
        band = np.exp(-((y - seam) / 0.045) ** 2) * 0.42
        bottom = np.clip((y - 0.8) / 0.2, 0, 1) ** 1.6 * 0.4
        top = np.clip(1 - y / 0.1, 0, 1) ** 1.5 * 0.15
        a = np.clip(band + bottom + top, 0, 0.85) * np.ones((1, ow))
        img = np.zeros((oh, ow, 4), np.uint8)
        img[..., 3] = (a * 255).astype(np.uint8)
        cv2.imwrite(path, img)
        return
    bottom = np.clip((y - 0.55) / 0.45, 0, 1) ** 1.6 * strength
    top = np.clip(1 - y / 0.14, 0, 1) ** 1.5 * 0.12
    x = np.linspace(-1, 1, ow)[None, :]
    side = np.clip(np.abs(x) - 0.75, 0, 1) / 0.25 * 0.10
    a = np.clip(bottom + top + side * (0.5 + y), 0, 0.85)
    img = np.zeros((oh, ow, 4), np.uint8)
    img[..., 3] = (a * 255).astype(np.uint8)
    cv2.imwrite(path, img)


# ---------------------------------------------------------------- 6. render
def full_composer(pos, geo, info):
    """Full layout: one face-following crop scaled to the output size."""
    ow, oh, cw, ch = geo["ow"], geo["oh"], geo["cw"], geo["ch"]
    sc = ow / cw

    def compose(f, i):
        c = pos.get(i)
        if geo["axis"] == "x":
            x0, y0 = (c if c is not None else info["w"] / 2) - cw / 2, 0.0
        else:
            x0, y0 = 0.0, (c if c is not None else info["h"] * 0.4) - ch / 2
        M = np.float32([[sc, 0, -x0 * sc], [0, sc, -y0 * sc]])
        return cv2.warpAffine(f, M, (ow, oh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return compose


def split_composer(px, py, geo, zoom):
    """Split layout: face-following facecam crop on top, fixed gameplay crop below."""
    ow, top_h, bot_h = geo["ow"], geo["top_h"], geo["bot_h"]
    fx, fy, fw, fh = geo["face"]
    win_w, win_h = fw / zoom, fh / zoom
    gx, gy, gw, gh = geo["game"]
    st, sg = ow / win_w, ow / gw
    Mg = np.float32([[sg, 0, -gx * sg], [0, sg, -gy * sg]])
    seam = np.zeros((4, ow, 3), np.uint8)

    def compose(f, i):
        cx = px.get(i, fx + fw / 2); cy = py.get(i, fy + fh / 2)
        x0, y0 = cx - win_w / 2, cy - win_h / 2
        Mt = np.float32([[st, 0, -x0 * st], [0, st, -y0 * st]])
        top = cv2.warpAffine(f, Mt, (ow, top_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        bot = cv2.warpAffine(f, Mg, (ow, bot_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        out = np.vstack([top, bot])
        out[top_h - 2:top_h + 2] = seam            # thin dark divider
        return out
    return compose


def render(src, info, segments, compose, geo, s: Settings, workdir, out_path, music, use_captions, job: Job):
    fps = info["fps"]
    ow, oh = geo["ow"], geo["oh"]
    ranges = [(round(a * fps), round(b * fps)) for a, b, _ in segments]
    n_out = sum(b - a for a, b in ranges)
    fc, inputs = [], ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ow}x{oh}", "-r", f"{fps:.6f}", "-i", "-",
                      "-i", src]
    # audio: exact frame-aligned trims with tiny fades so joins never click
    for k, (n0, n1) in enumerate(ranges):
        a, b = n0 / fps, n1 / fps
        fc.append(f"[1:a]atrim={a:.5f}:{b:.5f},asetpts=PTS-STARTPTS,"
                  f"afade=t=in:d=0.008,afade=t=out:st={max(0, b - a - 0.012):.5f}:d=0.012[a{k}]")
    fc.append("".join(f"[a{k}]" for k in range(len(ranges))) + f"concat=n={len(ranges)}:v=0:a=1[voice]")
    idx = 2
    vig = os.path.join(workdir, "vignette.png")
    if s.vignette:
        build_vignette(geo, vig); inputs += ["-i", "vignette.png"]
        fc.append(f"[0:v][{idx}:v]overlay=0:0:format=auto[v0]"); idx += 1
    else:
        fc.append("[0:v]null[v0]")
    if use_captions:
        fonts = os.path.join(workdir, "fonts"); os.makedirs(fonts, exist_ok=True)
        font = resolve_font(s.caption_font)
        if font["path"]: shutil.copy(font["path"], fonts)
        fc.append("[v0]ass=captions.ass:fontsdir=fonts,format=yuv420p[v]")
    else:
        fc.append("[v0]format=yuv420p[v]")
    if music and s.music:
        inputs += ["-stream_loop", "-1", "-i", music]
        gain = 10 ** (s.music_level_db / 20) * 0.5
        fc.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={gain:.4f}[mus]")
        fc.append("[voice]aformat=sample_rates=48000:channel_layouts=stereo,asplit[v1][v2]")
        fc.append("[mus][v2]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[duck]")
        fc.append("[v1][duck]amix=inputs=2:duration=first:normalize=0[mix]")
        fc.append("[mix]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
    else:
        fc.append("[voice]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
    with open(os.path.join(workdir, "graph.txt"), "w") as f:
        f.write(";\n".join(fc))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-/filter_complex", "graph.txt",
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out_path]
    errlog = open(os.path.join(workdir, "ffmpeg.log"), "w")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errlog, cwd=workdir)
    want = {}
    for n0, n1 in ranges:
        for n in range(n0, n1): want[n] = True
    last = max(want) if want else 0
    cap = cv2.VideoCapture(src)
    i = written = 0
    try:
        while i <= last:
            if i not in want:
                if not cap.grab(): break
                i += 1; continue
            ok, f = cap.read()
            if not ok: break
            frame = compose(f, i)
            proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            written += 1; i += 1
            if written % 20 == 0: job.stage("Rendering", written / max(n_out, 1))
        cap.release()
        proc.stdin.close()
    except BrokenPipeError:
        pass
    proc.wait(); errlog.close()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg render failed:\n" + open(os.path.join(workdir, "ffmpeg.log")).read()[-2000:])
    job.stage("Rendering", 1.0)


# ---------------------------------------------------------------- orchestration
def edit(src, out_path, instructions="", settings: Settings | None = None, music=None, job: Job | None = None,
         claude=None):
    from director import make_plan
    job = job or Job()
    s = settings or Settings()
    src = os.path.abspath(src); out_path = os.path.abspath(out_path)
    music = os.path.abspath(music) if music else None
    workdir = os.path.dirname(os.path.abspath(out_path))
    info = probe(src)
    if not info["has_audio"]:
        raise RuntimeError("This video has no audio track, so there is no speech to cut around.")
    job.log(f"Source: {info['w']}x{info['h']} @ {info['fps']:.2f} fps, {info['dur']:.1f}s")

    words = transcribe(src, job, s, info["dur"])
    if not words:
        job.log("No speech found; planning from the picture only.")
    with open(os.path.join(workdir, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump([asdict(w) for w in words], f, indent=1)

    job.stage("Planning the edit", 0)
    plan, s = make_plan(words, instructions, s, job, claude=claude,
                        video=dict(src=src, workdir=workdir, dur=info["dur"]))
    if plan.summary: job.log("Plan: " + plan.summary)
    segments = build_segments(words, plan, s, info["dur"])
    if not segments:
        raise RuntimeError("The edit plan kept nothing.")
    kept = sum(b - a for a, b, _ in segments)
    job.log(f"Cut list: {len(segments)} clips, {info['dur']:.1f}s -> {kept:.1f}s "
            f"({info['dur'] - kept:.1f}s of pauses/filler/off-topic removed).")
    job.stage("Planning the edit", 1)

    fps = info["fps"]
    ranges = [(round(a * fps), round(b * fps)) for a, b, _ in segments]
    frames = [n for a, b in ranges for n in range(a, b)]
    job.stage("Tracking faces", 0)
    if s.layout == "split":
        geo = split_setup(src, info, s)
        if geo["auto"]:
            job.log("Split layout: facecam " + ("found automatically" if geo["auto"]["face_found"]
                    else "not found, using the bottom-left corner") + ".")
        fx, fy, fw, fh = geo["face"]
        faces, cuts = analyze(src, info, ranges, job, roi=(fx, fy, fx + fw, fy + fh))
        cuts |= {n0 for n0, _ in ranges}
        zoom = 1.12                      # crop slightly inside the facecam box so it has room to follow
        ww, wh = fw / zoom, fh / zoom
        px = smooth_track(frames, faces, cuts, 0, ww, fx + ww / 2, fx + fw - ww / 2, fx + fw / 2, fps)
        py = smooth_track(frames, faces, cuts, 1, wh, fy + wh / 2, fy + fh - wh / 2, fy + fh / 2, fps)
        compose = split_composer(px, py, geo, zoom)
    else:
        geo = _crop_geometry(info["w"], info["h"], s.aspect)
        faces, cuts = analyze(src, info, ranges, job)
        cuts |= {n0 for n0, _ in ranges}
        compose = full_composer(camera_path(frames, faces, cuts, geo, info), geo, info)

    use_captions = s.captions or bool(plan.hook)
    if use_captions:
        n = build_ass(words, segments, plan, s, geo, os.path.join(workdir, "captions.ass"))
        if s.captions: job.log(f"Captions: {n} pages, {len(plan.highlights)} highlighted words.")
        if plan.hook: job.log(f"Hook text: \"{plan.hook['text']}\"" +
                              (f" (glowing: {', '.join(plan.hook['glow_words'])})" if plan.hook["glow_words"] else ""))
    job.stage("Rendering", 0)
    render(src, info, segments, compose, geo, s, workdir, out_path, music, use_captions, job)
    job.log(f"Done: {kept:.1f}s, {geo['ow']}x{geo['oh']}.")
    return dict(duration=kept, clips=len(segments), width=geo["ow"], height=geo["oh"])


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--instructions", default="")
    ap.add_argument("--music")
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--layout", default="full", choices=["full", "split"])
    a = ap.parse_args()
    edit(a.src, a.out, a.instructions, Settings(aspect=a.aspect, layout=a.layout), a.music)
