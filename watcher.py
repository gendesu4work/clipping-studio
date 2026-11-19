"""Watcher: Gemini watches the clip (picture + sound) and plans the edit by time, not just by words.

Transcript-only planning can't see what happens on screen, so silent action (a crash, a kill, a reaction)
looked like dead air and got cut. Here Gemini sees the video, picks the moments worth keeping as time
ranges, and still uses the transcript's word indices for caption highlights.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Literal

from pydantic import BaseModel, Field

from pipeline import COLORS, Plan

Color = Literal[tuple(COLORS)]  # type: ignore[valid-type]
Keep = Literal["keep"]


class Range(BaseModel):
    start: float = Field(description="Start, in seconds on the original video timeline")
    end: float = Field(description="End, in seconds on the original video timeline")
    why: str = Field(description="What happens in this part, a few words")


class Highlight(BaseModel):
    index: int = Field(description="Word index from the transcript")
    color: Color


class EditSettings(BaseModel):
    aspect: Literal["keep", "9:16", "3:4", "4:5", "1:1"]
    layout: Literal["keep", "full", "split", "framed"]
    captions: Literal["keep", "on", "off"]
    caption_position: Literal["keep", "top", "middle", "bottom"]
    vignette: Literal["keep", "on", "off"]
    music: Literal["keep", "on", "off"]


class Hook(BaseModel):
    text: str = Field(description='On-screen title, or "" for none')
    glow_words: list[str]
    color: Color
    seconds: float = Field(description="How long it stays on screen from the start; 0 = whole video")


class WatchPlan(BaseModel):
    keep: list[Range]
    highlights: list[Highlight]
    settings: EditSettings
    hook: Hook
    unsupported: list[str]
    summary: str


PROMPT = """You are the editor for a short-form vertical video (TikTok / Reels / Shorts). WATCH the whole video
carefully, picture AND sound, then plan the edit.

1. keep: the parts to keep, as time ranges in seconds on the original timeline, in order, not overlapping.
   Judge what is interesting from what you SEE and HEAR, not only from the words: keep the action itself
   (crashes, kills, fails, jump scares, wins, chases), the build-up just before it, and the reaction and
   laughter after it, even when nobody is talking. Cut boring stretches: waiting, menus, loading, dead air,
   repeated takes, off-topic chatter. The result must still make sense as a story: setup -> moment -> payoff.
   Never start or end in the middle of a sentence. If the creator asks for a target length, pick the best
   parts that fit it; otherwise keep every interesting part.
2. highlights: caption keywords to colour, about one per 5-7 kept words, by word index from the transcript.
   Colours: red = danger/negative/shocking, yellow = emphasis, green = positive/money/success,
   magenta = playful/flirty, orange = general energy, cyan = calm/tech.
3. settings: anything the creator said about format (aspect ratio, layout, captions on/off and position,
   vignette, music); "keep" for anything they didn't mention. layout "split" = facecam on top and gameplay
   below; "full" = one face-following crop.
4. hook: an on-screen title at the top, ONLY if the creator asks for hook text / a title / text on screen;
   otherwise text "". glow_words = words of the hook to glow, color = glow colour, seconds = 0 unless told.
5. unsupported: anything the creator asked for that the fields above can't do (sound effects, slow motion,
   B-roll...), each as a short phrase. Empty list if none. Zooms, shakes, flashes, circles, arrows, emojis and
   colour grading are added automatically when HEAVY EDIT is on, so never list those, and always keep the
   MUST-SHOW moments.
summary: one sentence about the edit. Only describe what the fields above actually do.

Creator's instructions:
{instructions}

Transcript ({n} words; each line starts with its time in seconds; format index:word):
{transcript}"""


def available() -> bool:
    from clipper import load_key
    return bool(load_key())


def _proxy(src, workdir, job):
    """A small 480p copy: uploads fast and costs fewer tokens, with the same timeline."""
    out = os.path.join(workdir, "watch_proxy.mp4")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                    "-vf", "scale=-2:480", "-r", "15", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
                    "-c:a", "aac", "-b:a", "64k", "-ac", "1", out], check=True)
    return out


def _snap(ranges, words, dur):
    """Clean the ranges and move their edges out of the middle of words."""
    out = []
    for a, b in sorted(ranges):
        a, b = max(0.0, a), min(dur, b)
        for w in words:
            if w.start < a < w.end: a = w.start
            if w.start < b < w.end: b = w.end
        a, b = max(0.0, a - 0.1), min(dur, b + 0.15)
        if b - a < 0.3: continue
        if out and a <= out[-1][1] + 0.05:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def watch_plan(src, workdir, words, instructions, dur, job):
    """Upload the clip to Gemini and get back a time-based Plan plus format settings."""
    from clipper import gemini_client, model_order, gemini_error, save_settings, load_key
    from director import _transcript_text
    from google.genai import types

    client = gemini_client()
    models = model_order(client)
    proxy = _proxy(src, workdir, job)
    job.log("Uploading the clip so Gemini can watch it...")
    uploaded = client.files.upload(file=proxy, config={"mime_type": "video/mp4"})
    try:
        for _ in range(150):
            if uploaded.state and uploaded.state.name == "ACTIVE": break
            if uploaded.state and uploaded.state.name == "FAILED":
                raise RuntimeError("Gemini couldn't process the video.")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        fps = 2.0 if dur <= 180 else 1.0 if dur <= 900 else 0.5   # short clips: finer timing
        part = types.Part(file_data=types.FileData(file_uri=uploaded.uri, mime_type="video/mp4"),
                          video_metadata=types.VideoMetadata(fps=fps))
        prompt = PROMPT.format(instructions=instructions.strip() or "(none - keep the best moments, cut the boring parts)",
                               n=len(words), transcript=_transcript_text(words) if words else "(no speech)")
        # a hung model shouldn't stall the edit for 5 minutes: short clips get a tighter time limit
        limit = 150_000 if dur <= 180 else 300_000
        config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=WatchPlan,
                                             http_options=types.HttpOptions(timeout=limit))
        last = None
        for i, model in enumerate(models):
            job.log(f"Gemini ({model}) is watching the clip...")
            try:
                resp = client.models.generate_content(
                    model=model, contents=types.Content(parts=[part, types.Part(text=prompt)]), config=config)
                data = resp.parsed or WatchPlan.model_validate_json(resp.text)
                break
            except Exception as e:
                last = gemini_error(e, model)
                quota = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
                if quota and "-pro" in model and "limit: 0" in str(e):
                    save_settings(pro_blocked_key=load_key()[-6:])
                if not (quota or "overloaded" in last) or i == len(models) - 1:
                    raise RuntimeError(last) from e
                job.log(f"  {model} {'has no free quota left' if quota else 'is busy'}; trying a backup model.")
        else:
            raise RuntimeError(last or "No Gemini model available.")
    finally:
        try: client.files.delete(name=uploaded.name)
        except Exception: pass
        try: os.remove(proxy)
        except OSError: pass

    times = _snap([(r.start, r.end) for r in data.keep], words, dur)
    if not times:
        raise RuntimeError("Gemini kept nothing")
    for r in data.keep:
        job.log(f"  keep {r.start:.1f}-{r.end:.1f}s: {r.why}")
    n = len(words)
    hook = None
    if data.hook.text.strip():
        hook = {"text": data.hook.text.strip(), "glow_words": list(data.hook.glow_words),
                "color": data.hook.color, "seconds": max(0.0, data.hook.seconds)}
    plan = Plan(keep=[], keep_times=times,
                highlights={h.index: h.color for h in data.highlights if 0 <= h.index < n},
                summary=data.summary, hook=hook,
                unsupported=[u.strip() for u in data.unsupported if u.strip()])
    return plan, data.settings.model_dump()
