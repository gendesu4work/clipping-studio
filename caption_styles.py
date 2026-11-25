"""Caption styles for ClipForge.

A style is a preset look (font, case, colours, outline, box, animation). The creator can pick one and then
tweak it (font, size, letters, words at a time, punctuation, colours, outline). Captions are laid out here
word by word, so every word is its own ASS event and can pop, bounce, rise or sit on its own box without
moving its neighbours.
"""
from __future__ import annotations

import io
import re

# Every key a style can set, with the value used when a style leaves it out.
DEFAULTS = dict(
    font=None,            # font id from font_catalog(); None = the app's default caption font
    case="upper",         # upper | lower | title | none
    wpp=None,             # words on screen at a time (None = the app default)
    size=1.0,             # multiplier on the base caption size
    spacing=0.06,         # letter spacing, share of the font size
    lh=1.12,              # line height, share of the font size
    punct=True,           # keep punctuation
    text="#ffffff",       # words
    pre=None,             # words not yet spoken (only when the whole page shows at once); None = text colour
    hi="#ffe600",         # highlight colour
    hi_mode="keyword",    # keyword: AI-picked words | active: the word being said | spoken: said words stay lit | none
    ai_colors=False,      # keyword words keep the colour the AI picked for them (unless the creator picks one)
    reveal="word",        # word: each word appears when it is said | page: the whole page shows, the active word changes
    anim="fade",          # fade | pop | bounce | rise | blur | type | none
    outline=0.05,         # outline width, share of the font size
    outline_color="#000000",
    outline_alpha=0,      # 0 = solid outline, 255 = invisible
    shadow=0.07,          # drop shadow distance, share of the font size
    glow=None,            # None | "all" | "hi" (only coloured words glow)
    glow_size=1.0,
    glow_color=None,      # None = the word's own colour
    box=None,             # None | "word" (a box behind the active word) | "line" (a box behind each line)
    box_color="#000000",
    box_text=None,        # word colour while it sits on its box (None = unchanged)
    box_alpha=0,          # 0 = solid box
    box_pad=0.2,          # padding around the word, share of the font size
    radius=0.32,          # corner radius, share of the box height
    echo=(),              # offset copies behind the text: [(dx, dy, "#colour")] in shares of the font size
    tilt=0,               # pages rotate this many degrees, alternating left and right
    sample="Caption",     # text on the style's tile in the app
)

STYLES = [
    dict(id="classic", name="Classic", sample="CLASSIC", spacing=0.16, ai_colors=True, glow="hi", outline_alpha=0x70),
    dict(id="glow", name="Glow", sample="glow", font="seguibl.ttf", case="lower", wpp=3, outline=0, shadow=0,
         glow="all", glow_size=1.5, glow_color="#b44bff", hi_mode="active", hi="#f3d9ff", spacing=0.02),
    dict(id="pill", name="Pill", sample="PILL", font="seguibl.ttf", wpp=3, reveal="page", hi_mode="none",
         box="word", box_color="#8b3dff", anim="pop", outline=0.04, shadow=0.04, spacing=0.02),
    dict(id="lime", name="Lime box", sample="LIME", font="ariblk.ttf", wpp=3, reveal="page", hi_mode="none",
         box="word", box_color="#9dff00", box_text="#000000", radius=0.14, anim="pop", outline=0.05, spacing=0.02),
    dict(id="duo", name="Duo", sample="duo tone", font="seguibl.ttf", case="lower", wpp=3, outline=0, shadow=0,
         hi_mode="none", anim="pop", spacing=0.02, echo=[(-0.045, -0.03, "#25f4ee"), (0.045, 0.03, "#fe2c55")]),
    dict(id="cyber", name="Cyber", sample="CYBER", font="consolab.ttf", spacing=0.12, text="#29f0ff", hi="#ff2bd6",
         hi_mode="active", reveal="page", anim="none", outline=0, shadow=0, glow="all", glow_size=0.8, wpp=3),
    dict(id="cine", name="Cine", sample="a quiet line", font="georgiai.ttf", case="lower", wpp=5, text="#fff8ec",
         outline=0, shadow=0.06, hi_mode="none", spacing=0.02, size=0.92, anim="fade"),
    dict(id="pop", name="Pop", sample="POP", font="impact.ttf", wpp=2, spacing=0.02, outline=0.09, shadow=0,
         ai_colors=True, anim="pop", size=1.22),
    dict(id="one", name="One word", sample="WORD", font="ariblk.ttf", wpp=1, size=1.45, outline=0.08,
         ai_colors=True, anim="pop", spacing=0.02),
    dict(id="karaoke", name="Karaoke", sample="SING IT", font="ariblk.ttf", wpp=4, reveal="page", hi_mode="spoken",
         anim="none", outline=0.07, spacing=0.02),
    dict(id="keyword", name="Keyword", sample="KEY WORD", font="ariblk.ttf", wpp=4, reveal="page", hi="#3dff6e",
         anim="none", outline=0.07, spacing=0.02),
    dict(id="loud", name="Loud", sample="BIG WIN", font="impact.ttf", wpp=2, size=1.35, outline=0.11, shadow=0.08,
         spacing=0.02, anim="pop", tilt=3),
    dict(id="hype", name="Hype", sample="HYPE", font="impact.ttf", wpp=3, text="#ffe600", hi="#ffffff",
         hi_mode="active", outline=0.09, size=1.2, spacing=0.02, anim="bounce"),
    dict(id="neon", name="Neon", sample="NEON", font="seguibl.ttf", wpp=3, text="#3ff3ff", glow="all",
         glow_color="#00b4ff", glow_size=1.3, outline=0, shadow=0, hi="#ffffff", hi_mode="active",
         reveal="page", anim="none", spacing=0.03),
    dict(id="bounce", name="Bounce", sample="BOUNCE IN", font="ariblk.ttf", wpp=3, hi="#ff8a00", hi_mode="active",
         anim="bounce", outline=0.07, spacing=0.02),
    dict(id="boxed", name="Subtitle box", sample="Subtitle", font="segoeuib.ttf", case="none", wpp=7, box="line",
         box_alpha=0x50, outline=0, shadow=0, size=0.82, spacing=0, reveal="page", anim="none", hi_mode="none",
         radius=0.22, box_pad=0.3),
    dict(id="clean", name="Clean", sample="Clean", font="seguisb.ttf", case="none", wpp=5, outline=0, shadow=0.07,
         spacing=0.01, hi_mode="none", size=0.95),
    dict(id="minimal", name="Minimal", sample="minimal", font="segoeui.ttf", case="lower", wpp=6, size=0.78,
         outline=0.03, shadow=0, spacing=0.02, hi_mode="none"),
    dict(id="softblur", name="Soft blur", sample="Soft blur", font="seguisb.ttf", case="none", wpp=4, anim="blur",
         outline=0, shadow=0.06, hi_mode="none", spacing=0.01),
    dict(id="rise", name="Word rise", sample="Word rise", font="arialbd.ttf", case="none", wpp=4, anim="rise",
         ai_colors=True, spacing=0.02),
    dict(id="typewriter", name="Typewriter", sample="type_", font="consolab.ttf", case="lower", wpp=5, anim="type",
         outline=0.04, shadow=0, spacing=0, hi_mode="none"),
    dict(id="comic", name="Comic", sample="COMIC!", font="comicbd.ttf", wpp=3, text="#ffe600", hi="#ff3b3b",
         outline=0.09, shadow=0.05, anim="pop", spacing=0.02),
]
BY_ID = {st["id"]: st for st in STYLES}
CASES = ("upper", "lower", "title", "none")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def public_styles():
    """The presets with every key filled in, for the app's style picker and preview."""
    return [dict(DEFAULTS, **{**st, "echo": [list(e) for e in st.get("echo", ())]}) for st in STYLES]


def look(style_id, custom, default_font, default_wpp, fonts):
    """The style the creator picked plus their tweaks -> one flat dict of settings."""
    lk = dict(DEFAULTS, **BY_ID.get(style_id or "", BY_ID["classic"]))
    c = custom if isinstance(custom, dict) else {}
    ids = {f["id"] for f in fonts}
    if c.get("font") in ids: lk["font"] = c["font"]
    elif lk["font"] not in ids: lk["font"] = default_font   # style's own font isn't on this PC
    try: lk["size"] *= min(200, max(40, float(c["size"]))) / 100
    except (KeyError, TypeError, ValueError): pass
    if c.get("case") in CASES: lk["case"] = c["case"]
    try: lk["wpp"] = min(5, max(1, int(c["wpp"])))
    except (KeyError, TypeError, ValueError): lk["wpp"] = lk["wpp"] or default_wpp
    if isinstance(c.get("punct"), bool): lk["punct"] = c["punct"]
    if HEX.match(str(c.get("text") or "")): lk["text"] = c["text"]
    if HEX.match(str(c.get("hi") or "")):
        lk["hi"] = c["hi"]; lk["ai_colors"] = False
        if lk["box"] == "word": lk["box_color"] = c["hi"]      # on box styles the box is the highlight
        elif lk["hi_mode"] == "none": lk["hi_mode"] = "keyword"   # picking a highlight colour turns highlights on
    if c.get("outline") is False:
        lk["outline"] = 0
    elif c.get("outline") is True:
        try: lk["outline_px"] = min(40, max(0, float(c["outline_px"])))
        except (KeyError, TypeError, ValueError): pass
        if not lk["outline"]: lk["outline"] = 0.06
        lk["outline_alpha"] = 0
    if HEX.match(str(c.get("outline_color") or "")): lk["outline_color"] = c["outline_color"]
    return lk


# ------------------------------------------------------------------ measuring
_metric_cache = {}


def _pil_font(path, fs):
    """A PIL font whose glyphs match libass at ASS size fs (libass sizes the font by its win ascent+descent)."""
    from PIL import ImageFont
    if path not in _metric_cache:
        try:
            from fontTools.ttLib import TTFont
            with open(path, "rb") as fh:
                f = TTFont(io.BytesIO(fh.read()), fontNumber=0)
            os2, upem = f["OS/2"], f["head"].unitsPerEm
            _metric_cache[path] = upem / max(1, os2.usWinAscent + os2.usWinDescent)
        except Exception:
            _metric_cache[path] = 0.9
    return ImageFont.truetype(path, max(1, round(fs * _metric_cache[path])))


def _measure(font, fs, spacing):
    pf = None
    if font.get("path"):
        try: pf = _pil_font(font["path"], fs)
        except Exception: pf = None

    def width(text):
        base = pf.getlength(text) if pf else len(text) * fs * 0.55
        return base + spacing * len(text)
    return width


# ------------------------------------------------------------------ helpers
def _bgr(hexcol):
    h = hexcol.lstrip("#").upper()
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def _rrect(w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    return (f"m {r:.0f} 0 l {w - r:.0f} 0 b {w:.0f} 0 {w:.0f} 0 {w:.0f} {r:.0f} l {w:.0f} {h - r:.0f} "
            f"b {w:.0f} {h:.0f} {w:.0f} {h:.0f} {w - r:.0f} {h:.0f} l {r:.0f} {h:.0f} b 0 {h:.0f} 0 {h:.0f} 0 {h - r:.0f} "
            f"l 0 {r:.0f} b 0 0 0 0 {r:.0f} 0")


def _case(text, case):
    if case == "upper": return text.upper()
    if case == "lower": return text.lower()
    if case == "title": return text[:1].upper() + text[1:].lower()
    return text


def _clean(text, punct):
    text = text.strip()
    if not punct:
        text = re.sub(r"[.,!?;:\"“”…]+", "", text).strip("-'‘’ ")
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def paginate(timeline, words, wpp):
    """[(word idx, start, end)] -> pages, breaking on size, long gaps and sentence ends."""
    pages, cur = [], []
    for i, st, en in timeline:
        if cur and (len(cur) >= wpp or st - cur[-1][2] > 0.6):
            pages.append(cur); cur = []
        cur.append((i, st, en))
        if words[i].text[-1:] in ".?!" and (len(cur) >= 2 or wpp == 1):
            pages.append(cur); cur = []
    if cur: pages.append(cur)
    return pages


# ------------------------------------------------------------------ events
def events(words, timeline, lk, font, ow, y, fs, t, highlights, colors, heavy=False):
    """ASS Dialogue lines for the spoken captions in look lk. Returns (lines, page count)."""
    fs = round(fs * lk["size"])
    wpp = lk["wpp"]
    if heavy: fs = round(fs * 1.18); wpp = min(wpp, 3)
    sp = fs * lk["spacing"]
    width = _measure(font, fs, sp)
    lh = fs * lk["lh"]
    max_w = ow * 0.86
    bord = lk.get("outline_px", fs * lk["outline"]) if lk["outline"] else 0
    shad = fs * lk["shadow"]
    oc, oa = _bgr(lk["outline_color"]), lk["outline_alpha"]
    out, pages = [], paginate(timeline, words, wpp)
    side = 1

    def kw_color(i):
        name = highlights.get(i)
        if lk["hi_mode"] != "keyword" or name not in colors: return None
        return _rgb_hex(colors[name]) if lk["ai_colors"] else lk["hi"]

    for p, page in enumerate(pages):
        toks = []
        for i, st, en in page:
            txt = _clean(_case(words[i].text, lk["case"]), lk["punct"])
            if txt: toks.append([i, st, en, txt, width(txt)])
        if not toks: continue
        ps = page[0][1]
        nxt = pages[p + 1][0][1] if p + 1 < len(pages) else page[-1][2] + 0.8
        pe = min(nxt, page[-1][2] + 0.8)
        # wrap into lines that fit the frame
        space = width(" ") + (fs * lk["box_pad"] if lk["box"] == "word" else 0)   # room for the word box
        lines, cur, cur_w = [], [], 0.0
        for tk in toks:
            add = tk[4] + (space if cur else 0)
            if cur and cur_w + add > max_w:
                lines.append((cur, cur_w)); cur, cur_w = [], 0.0; add = tk[4]
            cur.append(tk); cur_w += add
        if cur: lines.append((cur, cur_w))
        rot = ""
        if lk["tilt"]:
            rot = rf"\org({ow / 2:.0f},{y:.0f})\frz{lk['tilt'] * side:g}"; side = -side
        for k, (ln, lw) in enumerate(lines):
            cy = y + (k - (len(lines) - 1) / 2) * lh
            x = ow / 2 - lw / 2
            if lk["box"] == "line":
                pad = fs * lk["box_pad"]
                bw, bh = lw + 2 * pad, lh + pad * 0.6
                out.append(f"Dialogue: 0,{t(ps)},{t(pe)},Cap,,0,0,0,,"
                           + "{" + rf"\an7\pos({ow / 2 - bw / 2:.0f},{cy - bh / 2:.0f}){rot}\bord0\shad0\blur0"
                           rf"\1c{_bgr(lk['box_color'])}\1a&H{lk['box_alpha']:02X}&\fad(0,90)\p1" + "}"
                           + _rrect(bw, bh, bh * lk["radius"]) + r"{\p0}")
            for tk in ln:
                tk.append((x + tk[4] / 2, cy)); x += tk[4] + space
        for j, (i, st, en, txt, w, (cx, cy)) in enumerate(toks):
            act_end = toks[j + 1][1] if j + 1 < len(toks) else pe
            act_end = min(pe, max(act_end, st + 0.05))
            base = kw_color(i) or lk["text"]
            if lk["reveal"] == "page" and st - ps > 0.02:
                _word(out, lk, ps, st, cx, cy, txt, w, lk["pre"] or base, False, False, fs, sp, bord, shad, oc, oa,
                      rot, t, heavy, pe)
            fill = lk["hi"] if lk["hi_mode"] in ("active", "spoken") else base
            _word(out, lk, st, act_end, cx, cy, txt, w, fill, True, True, fs, sp, bord, shad, oc, oa, rot, t, heavy, pe,
                  coloured=fill != lk["text"])
            if act_end < pe - 0.01:
                post = lk["hi"] if lk["hi_mode"] == "spoken" else base
                _word(out, lk, act_end, pe, cx, cy, txt, w, post, False, False, fs, sp,
                      bord, shad, oc, oa, rot, t, heavy, pe, coloured=post != lk["text"])
    return out, len(pages)


def _word(out, lk, a, b, cx, cy, txt, w, fill, active, entering, fs, sp, bord, shad, oc, oa, rot, t, heavy, pe,
          coloured=False):
    """One word for one stretch of time (before / while / after it is said)."""
    if b - a < 0.01: return
    anim = lk["anim"] if entering else "none"
    if lk["reveal"] == "page" and anim not in ("pop", "bounce"):
        anim = "none"    # the word is already on screen: only a pop or a hop reads as "now"
    on_box = active and lk["box"] == "word"
    col = lk["box_text"] if on_box and lk["box_text"] else fill
    fade_out = r"\fad(0,90)" if b >= pe - 0.001 else ""
    glow = lk["glow"] == "all" or (lk["glow"] == "hi" and coloured)
    gcol = _bgr(lk["glow_color"] or col)
    gs = lk["glow_size"]
    edge = (rf"\3c{gcol}\3a&H90&\bord{fs * 0.09 * gs + bord:.1f}\blur{fs * 0.3 * gs:.1f}" if glow and not on_box
            else r"\bord0\blur0\3a&H00&" if on_box
            else rf"\3c{oc}\3a&H{oa:02X}&\bord{bord:.1f}\blur{min(bord, fs * 0.08) if bord else 0:.1f}")
    pos = rf"\pos({cx:.0f},{cy:.0f})"
    move, move_dy, move_ms = "", 0, 0
    intro = ""
    if anim == "fade" and lk["reveal"] == "word":
        # word fades in grey -> its colour
        e3 = re.search(r"\\3a&H(..)&", edge).group(1)
        edge = edge.replace(rf"\3a&H{e3}&", "")
        intro = (rf"\1a&HFF&\3a&HFF&\4a&HFF&\1c&HC8C8C8&\t(0,70,\1a&H55&)"
                 rf"\t(70,230,\1a&H00&\3a&H{e3}&\4a&H80&\1c{_bgr(col)})")
    elif anim == "fade":
        intro = r"\fad(120,0)"
    elif anim == "pop":
        intro = r"\fscx60\fscy60\t(0,90,\fscx112\fscy112)\t(90,170,\fscx100\fscy100)"
    elif anim == "bounce":
        move_dy, move_ms = -fs * 0.35, 140
        intro = r"\fscy120\t(0,140,\fscy88)\t(140,240,\fscy100)" + (r"\alpha&HFF&\t(0,60,\alpha&H00&)" if lk["reveal"] == "word" else "")
    elif anim == "rise":
        move_dy, move_ms = fs * 0.45, 190
        intro = r"\alpha&HFF&\t(0,150,\alpha&H00&)"
    elif anim == "blur":
        intro = rf"\alpha&HFF&\blur{fs * 0.4:.1f}\t(0,240,\alpha&H00&\blur0)"
    if move_ms:
        move = rf"\move({cx:.0f},{cy + move_dy:.0f},{cx:.0f},{cy:.0f},0,{move_ms})"
    if heavy and anim in ("fade", "none") and entering:
        intro += r"\fscx100\fscy100\t(0,80,\fscx125\fscy125)\t(80,180,\fscx100\fscy100)"
    body = txt
    if anim == "type":
        per = min(70, max(25, (b - a) * 1000 * 0.6 / max(1, len(txt))))
        body = "".join("{" + rf"\alpha&HFF&\t({int(k * per)},{int(k * per) + 1},\alpha&H00&)" + "}" + ch
                       for k, ch in enumerate(txt))
    head = (rf"\an5{move or pos}{rot}\fs{fs}\fsp{sp:.1f}\1c{_bgr(col)}{edge}\4c&H000000&\4a&H80&\shad{shad:.1f}"
            + fade_out + intro)
    if on_box:
        pad = fs * lk["box_pad"]
        bw, bh = w + 2 * pad, fs * 1.08 + pad * 0.5
        pop = r"\fscx70\fscy70\t(0,90,\fscx106\fscy106)\t(90,160,\fscx100\fscy100)" if lk["anim"] == "pop" else ""
        out.append(f"Dialogue: 1,{t(a)},{t(b)},Cap,,0,0,0,,"
                   + "{" + rf"\an5\pos({cx:.0f},{cy + fs * 0.04:.0f}){rot}\bord0\shad0\blur0\1c{_bgr(lk['box_color'])}"
                   rf"\1a&H{lk['box_alpha']:02X}&{fade_out}{pop}\p1" + "}" + _rrect(bw, bh, bh * lk["radius"]) + r"{\p0}")
    e_intro = r"\alpha&HFF&\t(0,230,\alpha&H00&)" if anim == "fade" and lk["reveal"] == "word" else intro
    for dx, dy, ecol in lk["echo"]:
        ex, ey = cx + dx * fs, cy + dy * fs
        ep = (rf"\move({ex:.0f},{ey + (move_dy or 0):.0f},{ex:.0f},{ey:.0f},0,{move_ms})" if move
              else rf"\pos({ex:.0f},{ey:.0f})")
        out.append(f"Dialogue: 2,{t(a)},{t(b)},Cap,,0,0,0,,"
                   + "{" + rf"\an5{ep}{rot}\fs{fs}\fsp{sp:.1f}\1c{_bgr(ecol)}\bord0\shad0{fade_out}{e_intro}" + "}" + body)
    out.append(f"Dialogue: 3,{t(a)},{t(b)},Cap,,0,0,0,,{{{head}}}{body}")
