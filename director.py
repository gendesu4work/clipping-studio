"""Director: turns the transcript + your plain-English instructions into an edit plan.

Uses Claude when an Anthropic API key is available (smart story cuts, semantic
keyword highlights); otherwise falls back to built-in rules.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace

from pipeline import Plan, Settings, COLORS

FILLERS = {"um", "uh", "umm", "uhh", "erm", "er", "ah", "hmm", "mm"}
STOP = set("""a an the and or but so to of in on at for with is are was were be been am i you he she it we they me
him her them my your his its our their this that these those there here not no yes do does did have has had just
like really very can could would should will gonna wanna get got go going what when where who why how if then than
because about from up down out over into all some any more most much many one oh okay ok yeah well know think""".split())

SYSTEM = """You are the editor for short-form vertical videos (TikTok / Reels / Shorts) cut from interviews and talking-head footage.
You receive a word-level transcript where every word has an index, plus the creator's instructions.
Decide:
1. keep: which word ranges to keep, in chronological order, as inclusive index ranges. Remove filler words (um, uh),
   false starts, stutters, repeated takes and off-topic tangents. If the creator asks for a target length or for
   "the best part", choose the most engaging self-contained story (hook first, payoff at the end) and drop the rest.
   With no length instruction, keep the whole story and only remove filler and false starts.
   Never cut mid-sentence in a way that changes the meaning.
2. highlights: caption keywords to colour, about one per 5-7 kept words: the punchiest word or short phrase (each
   word of a phrase listed separately). Colours carry meaning: red = danger/negative/shocking, yellow = emphasis,
   green = positive/money/success, magenta = playful/flirty, orange = general energy, cyan = calm/tech.
3. settings: apply anything the creator said about format (aspect ratio, layout, captions on/off and position,
   vignette, music). layout "split" = facecam/reaction on top and gameplay below (gaming clips); "full" = one
   face-following crop. Use "keep" for anything they did not mention.
4. hook: an on-screen title shown at the top of the video. Fill it in when the creator asks for hook text, a title,
   a headline or text on screen; otherwise leave text "". Keep it short and punchy (max ~8 words), in the creator's
   words if they gave them. glow_words = the words of the hook that should glow (exact words from the hook text,
   e.g. the ones the creator asked to glow or highlight), color = the glow colour. seconds = how long it stays on
   screen from the start (0 = the whole video; use 0 unless the creator says otherwise).
5. unsupported: anything the creator asked for that this editor can NOT do with the fields above (for example
   zooms, sound effects, stickers, emojis, transitions, slow motion, filters, B-roll, memes), each as a short
   phrase quoting their request. Empty list if everything was handled.
Write a one-sentence summary of the edit you made. Only describe what the fields above actually do: never claim an
effect that is listed in unsupported."""

SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "array", "items": {
            "type": "object",
            "properties": {"from": {"type": "integer"}, "to": {"type": "integer"}},
            "required": ["from", "to"], "additionalProperties": False}},
        "highlights": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "color": {"type": "string", "enum": list(COLORS)}},
            "required": ["index", "color"], "additionalProperties": False}},
        "settings": {
            "type": "object",
            "properties": {
                "aspect": {"type": "string", "enum": ["keep", "9:16", "3:4", "4:5", "1:1"]},
                "layout": {"type": "string", "enum": ["keep", "full", "split"]},
                "captions": {"type": "string", "enum": ["keep", "on", "off"]},
                "caption_position": {"type": "string", "enum": ["keep", "top", "middle", "bottom"]},
                "vignette": {"type": "string", "enum": ["keep", "on", "off"]},
                "music": {"type": "string", "enum": ["keep", "on", "off"]},
            },
            "required": ["aspect", "layout", "captions", "caption_position", "vignette", "music"],
            "additionalProperties": False},
        "hook": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "glow_words": {"type": "array", "items": {"type": "string"}},
                "color": {"type": "string", "enum": list(COLORS)},
                "seconds": {"type": "number"},
            },
            "required": ["text", "glow_words", "color", "seconds"],
            "additionalProperties": False},
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["keep", "highlights", "settings", "hook", "unsupported", "summary"],
    "additionalProperties": False,
}


def _norm(t):
    return re.sub(r"[^\w']", "", t.lower())


def _transcript_text(words):
    lines, cur = [], []
    for i, w in enumerate(words):
        cur.append(f"{i}:{w.text}")
        if w.text[-1:] in ".?!" or len(cur) >= 14:
            lines.append(f"[{words[i - len(cur) + 1].start:.1f}s] " + " ".join(cur)); cur = []
    if cur: lines.append(f"[{words[len(words) - len(cur)].start:.1f}s] " + " ".join(cur))
    return "\n".join(lines)


# ------------------------------------------------------------------ rules
LENGTH_RE = r"(\d+)\s*(s|sec|secs|second|seconds|min|minute|minutes)\b"

# Phrases the built-in rules understand: (pattern, how it changes the settings).
TEXT_RULES = [
    (r"9\s*[:x/]\s*16|vertical|tiktok|reels?\b|shorts?\b", lambda s, m: replace(s, aspect="9:16")),
    (r"3\s*[:x/]\s*4", lambda s, m: replace(s, aspect="3:4")),
    (r"4\s*[:x/]\s*5", lambda s, m: replace(s, aspect="4:5")),
    (r"1\s*[:x/]\s*1|square", lambda s, m: replace(s, aspect="1:1")),
    (r"split|face ?cam|webcam|gameplay|gaming|reaction (on )?top", lambda s, m: replace(s, layout="split")),
    (r"full ?(frame|screen)|no split", lambda s, m: replace(s, layout="full")),
    (r"no (sub|caption)|without (sub|caption)|captions? off", lambda s, m: replace(s, captions=False)),
    (r"no vignette|without (a )?vignette|vignette off", lambda s, m: replace(s, vignette=False)),
    (r"no music|without music|music off", lambda s, m: replace(s, music=False)),
    (r"captions?\s+(?:at|on|to)\s+(?:the\s+)?(top|middle|center|bottom)",
     lambda s, m: replace(s, caption_position={"center": "middle"}.get(m.group(1), m.group(1)))),
    (r"keep (the )?pauses|don'?t cut pauses|slower pace", lambda s, m: replace(s, max_pause=1.0)),
    (r"tight|fast pace|snappy|aggressive", lambda s, m: replace(s, max_pause=0.2)),
]
# Also understood, but handled in rules_plan rather than as a setting.
PLAN_RULES = [LENGTH_RE, r"filler|\bums?\b|\buhs?\b|stutter|pauses|silence"]
# Words that carry no request on their own ("please", "make it", ...).
NOISE = re.compile(r"^(please|pls|thanks?( you)?|thx|ok(ay)?|also|just|make it|do it|edit it|the video|this|again)$")


def _apply_text_settings(text, s: Settings) -> Settings:
    t = text.lower()
    for pat, apply in TEXT_RULES:
        m = re.search(pat, t)
        if m: s = apply(s, m)
    return s


def ununderstood_parts(text):
    """The pieces of the instructions that the built-in rules can't act on."""
    patterns = [p for p, _ in TEXT_RULES] + PLAN_RULES
    parts = re.split(r"[.;,!?\n]+|\s+(?:and|then|but|also)\s+", text)
    out = []
    for p in parts:
        p = p.strip(" '\"-")
        if len(p) < 3 or NOISE.match(p.lower()): continue
        if not any(re.search(pat, p.lower()) for pat in patterns): out.append(p)
    return out


def _target_seconds(text):
    m = re.search(LENGTH_RE, text.lower())
    if not m: return None
    n = int(m.group(1))
    return n * 60 if m.group(2).startswith("min") else n


def rules_plan(words, instructions, s: Settings):
    keep_idx = [i for i, w in enumerate(words) if _norm(w.text) not in FILLERS]
    # drop immediate repeats ("I I I think")
    keep_idx = [i for k, i in enumerate(keep_idx)
                if k == 0 or _norm(words[i].text) != _norm(words[keep_idx[k - 1]].text) or len(_norm(words[i].text)) > 3]
    target = _target_seconds(instructions)
    if target:
        total, cut_at = 0.0, len(keep_idx)
        for k, i in enumerate(keep_idx):
            total += words[i].end - words[i].start + 0.08
            if total > target:
                # back up to the last sentence end
                for j in range(k, 0, -1):
                    if words[keep_idx[j]].text[-1:] in ".?!": cut_at = j + 1; break
                else:
                    cut_at = k
                break
        keep_idx = keep_idx[:cut_at]
    ranges = []
    for i in keep_idx:
        if ranges and ranges[-1][1] == i - 1: ranges[-1][1] = i
        else: ranges.append([i, i])
    # highlights: longest non-stopword in each ~6-word window
    palette = ["yellow", "red", "magenta", "orange", "green"]
    hl, c = {}, 0
    for k in range(0, len(keep_idx), 6):
        win = [i for i in keep_idx[k:k + 6] if _norm(words[i].text) not in STOP and len(_norm(words[i].text)) >= 4]
        if win:
            best = max(win, key=lambda i: len(_norm(words[i].text)))
            hl[best] = palette[c % len(palette)]; c += 1
    return Plan(keep=[tuple(r) for r in ranges], highlights=hl,
                summary="Built-in rules: removed filler words and stutters" + (f", trimmed to ~{target}s" if target else "") + ".")


# ------------------------------------------------------------------ Claude
DEFAULT_MODEL = "claude-opus-5-5"


def make_client(claude: dict | None):
    """Anthropic client from the saved settings: key, plus an optional reseller address (e.g. Coarena)."""
    import anthropic
    c = claude or {}
    kw = {}
    if c.get("api_key"): kw["api_key"] = c["api_key"]
    if c.get("base_url"): kw["base_url"] = c["base_url"]
    return anthropic.Anthropic(**kw)


def check_key(claude: dict) -> str | None:
    """Make one tiny request with these settings. Returns None if it works, else a readable error."""
    import anthropic
    try:
        make_client(claude).messages.create(model=claude.get("model") or DEFAULT_MODEL, max_tokens=5,
                                            messages=[{"role": "user", "content": "Say ok"}])
        return None
    except anthropic.AuthenticationError:
        return "The key was rejected (invalid key)."
    except anthropic.PermissionDeniedError as e:
        return f"The key isn't allowed to do this: {_short(e)}"
    except anthropic.NotFoundError as e:
        return f"Model or address not found. Check the model name and API address. ({_short(e)})"
    except anthropic.RateLimitError:
        return None  # key works, just busy
    except anthropic.APIConnectionError:
        return "Couldn't reach the API address. Check it and your internet connection."
    except anthropic.APIStatusError as e:
        if e.status_code == 402 or "credit" in str(e).lower() or "balance" in str(e).lower():
            return f"The key works but has no credit left. ({_short(e)})"
        return f"Error {e.status_code}: {_short(e)}"


def _short(e):
    return (str(e).splitlines() or [""])[0][:200]


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise RuntimeError("Claude's reply had no JSON plan")
    return json.loads(m.group(0))


def claude_plan(words, instructions, claude, job):
    client = make_client(claude)
    model = (claude or {}).get("model") or DEFAULT_MODEL
    user = (f"Creator's instructions:\n{instructions.strip() or '(none - make a clean, tight edit)'}\n\n"
            f"Transcript ({len(words)} words, total {words[-1].end:.1f}s; format index:word):\n{_transcript_text(words)}")
    if (claude or {}).get("base_url"):
        # Resellers often don't support Anthropic's newer options, so ask for JSON in plain words.
        job.log(f"Asking Claude ({model}, via {claude['base_url']}) to plan the edit...")
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM + "\n\nReply with ONLY a JSON object (no prose, no code fences) matching this JSON schema:\n"
                   + json.dumps(SCHEMA),
            messages=[{"role": "user", "content": user}],
        )
    else:
        job.log("Asking Claude to plan the edit...")
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to plan this edit")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's plan was cut off")
    data = _parse_json(next(b.text for b in resp.content if b.type == "text"))
    for k, v in {"aspect": "keep", "layout": "keep", "captions": "keep", "caption_position": "keep",
                 "vignette": "keep", "music": "keep"}.items():
        data.setdefault("settings", {}).setdefault(k, v)
    data.setdefault("highlights", []); data.setdefault("summary", ""); data.setdefault("unsupported", [])
    n = len(words)
    keep = [(max(0, r["from"]), min(n - 1, r["to"])) for r in data["keep"] if r["from"] <= r["to"]]
    hl = {h["index"]: h["color"] for h in data["highlights"] if 0 <= h["index"] < n}
    h = data.get("hook") or {}
    hook = None
    if str(h.get("text") or "").strip():
        hook = {"text": str(h["text"]).strip(), "glow_words": [str(w) for w in h.get("glow_words") or []],
                "color": h.get("color") if h.get("color") in COLORS else "yellow",
                "seconds": max(0.0, float(h.get("seconds") or 0))}
    unsupported = [str(u).strip() for u in data["unsupported"] if str(u).strip()]
    return Plan(keep=keep, highlights=hl, summary=data["summary"], hook=hook, unsupported=unsupported), data["settings"]


# The AI may only change a setting when the instructions talk about it; otherwise what the creator picked
# in the app wins (models sometimes "helpfully" move captions or change the layout unasked).
SETTING_TOPICS = {
    "aspect": r"\d\s*[:x/]\s*\d|vertical|horizontal|portrait|landscape|square|aspect|ratio|format|tiktok|reels?\b|shorts?\b",
    "layout": r"split|face ?cam|webcam|\bcam\b|layout|full ?(frame|screen)|gameplay|reaction",
    "captions": r"caption|subtitle|\bsubs?\b",
    "caption_position": r"caption|subtitle|\bsubs?\b",
    "vignette": r"vignette",
    "music": r"music|song|\bbeat\b|soundtrack",
}


def _apply_settings(st, s: Settings, instructions: str = "") -> Settings:
    t = (instructions or "").lower()
    def asked(k): return st.get(k, "keep") != "keep" and re.search(SETTING_TOPICS[k], t)
    if asked("aspect"): s = replace(s, aspect=st["aspect"])
    if asked("layout"): s = replace(s, layout=st["layout"])
    if asked("captions"): s = replace(s, captions=st["captions"] == "on")
    if asked("caption_position"): s = replace(s, caption_position=st["caption_position"])
    if asked("vignette"): s = replace(s, vignette=st["vignette"] == "on")
    if asked("music"): s = replace(s, music=st["music"] == "on")
    return _apply_text_settings(instructions, s)   # the creator's own clear wording beats the AI's reading


def make_plan(words, instructions, s: Settings, job, claude=None, video=None):
    s = _apply_text_settings(instructions or "", s)
    # 1) Gemini watches the clip itself, so silent action isn't mistaken for dead air
    if video:
        try:
            import watcher
            if watcher.available():
                plan, st = watcher.watch_plan(video["src"], video["workdir"], words, instructions or "",
                                              video["dur"], job)
                if plan.unsupported: job.log("Ignored: " + " | ".join(plan.unsupported))
                return plan, _apply_settings(st, s, instructions)
        except Exception as e:
            job.log(f"Gemini couldn't watch the clip ({_short(e)}); planning from the transcript instead.")
    if not words:
        raise RuntimeError("No speech found in the video, and Gemini couldn't watch it.")
    # 2) Claude (or a reseller model) reads the transcript
    try:
        import anthropic  # noqa: F401
        plan, st = claude_plan(words, instructions or "", claude, job)
        s = _apply_settings(st, s, instructions)
        if not plan.keep: raise RuntimeError("Claude returned an empty cut list")
        if plan.unsupported:
            job.log("Ignored: " + " | ".join(plan.unsupported))
        return plan, s
    except Exception as e:  # no key, network down, refusal... -> rules
        msg = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
        if "api_key" in msg.lower() or "auth" in msg.lower() or "credential" in msg.lower():
            msg = "no Anthropic API key set"
        job.log(f"Claude not used ({msg}); using built-in rules.")
        plan = rules_plan(words, instructions or "", s)
        skipped = ununderstood_parts(instructions or "")
        if skipped:
            job.log("Ignored: " + " | ".join(skipped))
        return plan, s
