"""ClipForge editing engine.

Turns a talking-head / interview video into a short:
  transcribe -> plan (director) -> cut dead air -> face-follow reframe
  -> word-by-word captions -> vignette -> music bed -> loudness normalize.
"""
from __future__ import annotations

import json
import math
import os
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
    vignette: bool = True
    music: bool = True                 # only used if a music file is supplied
    music_level_db: float = -12.0      # music bed relative to the voice
    max_pause: float = 0.35            # gaps longer than this get cut
    pad_start: float = 0.06
    pad_end: float = 0.12
    words_per_page: int = 6
    whisper_model: str = "small"


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Plan:
    keep: list = field(default_factory=list)        # [(from_idx, to_idx)] inclusive
    highlights: dict = field(default_factory=dict)  # word idx -> color name
    summary: str = ""


class Job:
    """Progress sink. The server replaces these callbacks."""
    def log(self, msg: str): print(msg, flush=True)
    def stage(self, name: str, frac: float = 0.0): print(f"[{name}] {frac:.0%}", flush=True)


# ---------------------------------------------------------------- helpers
def run(cmd, **kw):
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
    """Kept words -> list of (start, end, [word indices]) with dead air removed."""
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


def analyze(src, info, frame_ranges, job: Job):
    """Pass 1: face centers + scene-cut flags for every kept frame."""
    want = np.zeros(int(info["dur"] * info["fps"]) + 10, bool)
    for n0, n1 in frame_ranges: want[n0:n1] = True
    det_step = max(1, round(info["fps"] / 15))
    scale = 640 / max(info["w"], info["h"])
    dw, dh = int(info["w"] * scale), int(info["h"] * scale)
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
        thumb = cv2.cvtColor(cv2.resize(small, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev_hist is not None and want[i] and float(np.mean(np.abs(thumb - prev_hist))) > 12:
            cuts.add(i)
        prev_hist = thumb
        if want[i]:
            if i % det_step == 0:
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
    """Virtual camera: hold inside a deadzone, glide with a critically damped
    spring when the subject leaves it, snap on camera switches/edit points."""
    fps = info["fps"]
    span = geo["cw"] if geo["axis"] == "x" else geo["ch"]
    full = info["w"] if geo["axis"] == "x" else info["h"]
    lo, hi = span / 2, full - span / 2
    k = 0 if geo["axis"] == "x" else 1
    default = full / 2 if geo["axis"] == "x" else full * 0.4
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


# ---------------------------------------------------------------- 4. captions (ASS)
def _ass_color(rgb, alpha=0):
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _bgr(rgb):
    r, g, b = rgb
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words, segments, plan: Plan, s: Settings, geo, path):
    ow, oh = geo["ow"], geo["oh"]
    fs = round(oh * 0.032) if s.aspect == "9:16" else round(oh * 0.036)
    y = {"top": 0.22, "middle": 0.52, "bottom": 0.78}.get(s.caption_position, 0.52) * oh
    max_chars = 18 if ow / oh < 0.7 else 24
    # map source word times -> output timeline
    timeline, t_out = [], 0.0
    for a, b, idx in segments:
        for i in idx:
            w = words[i]
            timeline.append((i, t_out + max(0, w.start - a), t_out + min(b - a, w.end - a)))
        t_out += b - a
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
        f"Style: Cap,Arial,{fs},&H00FFFFFF,&H00FFFFFF,&H90000000,&H80000000,-1,0,0,0,100,100,{fs * 0.16:.1f},0,1,{fs * 0.05:.1f},{fs * 0.07:.1f},5,40,40,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for p, page in enumerate(pages):
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
    bottom = np.clip((y - 0.55) / 0.45, 0, 1) ** 1.6 * strength
    top = np.clip(1 - y / 0.14, 0, 1) ** 1.5 * 0.12
    x = np.linspace(-1, 1, ow)[None, :]
    side = np.clip(np.abs(x) - 0.75, 0, 1) / 0.25 * 0.10
    a = np.clip(bottom + top + side * (0.5 + y), 0, 0.85)
    img = np.zeros((oh, ow, 4), np.uint8)
    img[..., 3] = (a * 255).astype(np.uint8)
    cv2.imwrite(path, img)


# ---------------------------------------------------------------- 6. render
def render(src, info, segments, pos, geo, s: Settings, workdir, out_path, music, use_captions, job: Job):
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
        for fn in ("arialbd.ttf", "arial.ttf"):
            p = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", fn)
            if os.path.exists(p): shutil.copy(p, fonts)
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
    cw, ch = geo["cw"], geo["ch"]
    sc = ow / cw
    i = written = 0
    try:
        while i <= last:
            if i not in want:
                if not cap.grab(): break
                i += 1; continue
            ok, f = cap.read()
            if not ok: break
            c = pos.get(i)
            if geo["axis"] == "x":
                x0, y0 = (c if c is not None else info["w"] / 2) - cw / 2, 0.0
            else:
                x0, y0 = 0.0, (c if c is not None else info["h"] * 0.4) - ch / 2
            M = np.float32([[sc, 0, -x0 * sc], [0, sc, -y0 * sc]])
            frame = cv2.warpAffine(f, M, (ow, oh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            proc.stdin.write(frame.tobytes())
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
         api_key=None):
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
        raise RuntimeError("No speech found in the video.")
    with open(os.path.join(workdir, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump([asdict(w) for w in words], f, indent=1)

    job.stage("Planning the edit", 0)
    plan, s = make_plan(words, instructions, s, job, api_key=api_key)
    if plan.summary: job.log("Plan: " + plan.summary)
    segments = build_segments(words, plan, s, info["dur"])
    if not segments:
        raise RuntimeError("The edit plan kept nothing.")
    kept = sum(b - a for a, b, _ in segments)
    job.log(f"Cut list: {len(segments)} clips, {info['dur']:.1f}s -> {kept:.1f}s "
            f"({info['dur'] - kept:.1f}s of pauses/filler/off-topic removed).")
    job.stage("Planning the edit", 1)

    geo = _crop_geometry(info["w"], info["h"], s.aspect)
    fps = info["fps"]
    ranges = [(round(a * fps), round(b * fps)) for a, b, _ in segments]
    job.stage("Tracking faces", 0)
    faces, cuts = analyze(src, info, ranges, job)
    cuts |= {n0 for n0, _ in ranges}
    frames = [n for a, b in ranges for n in range(a, b)]
    pos = camera_path(frames, faces, cuts, geo, info)

    use_captions = s.captions
    if use_captions:
        n = build_ass(words, segments, plan, s, geo, os.path.join(workdir, "captions.ass"))
        job.log(f"Captions: {n} pages, {len(plan.highlights)} highlighted words.")
    job.stage("Rendering", 0)
    render(src, info, segments, pos, geo, s, workdir, out_path, music, use_captions, job)
    job.log(f"Done: {kept:.1f}s, {geo['ow']}x{geo['oh']}.")
    return dict(duration=kept, clips=len(segments), width=geo["ow"], height=geo["oh"])


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--instructions", default="")
    ap.add_argument("--music")
    ap.add_argument("--aspect", default="9:16")
    a = ap.parse_args()
    edit(a.src, a.out, a.instructions, Settings(aspect=a.aspect), a.music)
