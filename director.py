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
Write a one-sentence summary of the edit you made."""

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
        "summary": {"type": "string"},
    },
    "required": ["keep", "highlights", "settings", "summary"],
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
def claude_plan(words, instructions, api_key, job):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    user = (f"Creator's instructions:\n{instructions.strip() or '(none - make a clean, tight edit)'}\n\n"
            f"Transcript ({len(words)} words, total {words[-1].end:.1f}s; format index:word):\n{_transcript_text(words)}")
    job.log("Asking Claude to plan the edit...")
    resp = client.beta.messages.create(
        model="claude-opus-5-5",
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
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    n = len(words)
    keep = [(max(0, r["from"]), min(n - 1, r["to"])) for r in data["keep"] if r["from"] <= r["to"]]
    hl = {h["index"]: h["color"] for h in data["highlights"] if 0 <= h["index"] < n}
    return Plan(keep=keep, highlights=hl, summary=data["summary"]), data["settings"]


def make_plan(words, instructions, s: Settings, job, api_key=None):
    s = _apply_text_settings(instructions or "", s)
    try:
        import anthropic  # noqa: F401
        plan, st = claude_plan(words, instructions or "", api_key, job)
        if st["aspect"] != "keep": s = replace(s, aspect=st["aspect"])
        if st.get("layout", "keep") != "keep": s = replace(s, layout=st["layout"])
        if st["captions"] != "keep": s = replace(s, captions=st["captions"] == "on")
        if st["caption_position"] != "keep": s = replace(s, caption_position=st["caption_position"])
        if st["vignette"] != "keep": s = replace(s, vignette=st["vignette"] == "on")
        if st["music"] != "keep": s = replace(s, music=st["music"] == "on")
        if not plan.keep: raise RuntimeError("Claude returned an empty cut list")
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
