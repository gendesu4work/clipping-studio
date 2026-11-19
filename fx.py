"""Heavy-edit effects for ClipForge, drawn on each output frame.

Punch-in zooms, camera shake, white flashes on cuts, red circles + arrows on the subject, colour emoji pops,
and the "meme frame" layout (black frame, title on top, square video, channel handle below).
Everything is local: no AI or network needed to draw it.
"""
from __future__ import annotations

import math
import os
import random

import cv2
import numpy as np

EMOJI_FONT = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "seguiemj.ttf")
KINDS = ("zoom", "circle", "shake", "flash", "emoji")
COLOR_EMOJI = {"red": "💀", "green": "🤑", "yellow": "🔥", "magenta": "😏", "orange": "😂", "cyan": "🤯"}
_emoji_cache: dict = {}


def emoji_image(ch, size):
    """A colour emoji as a BGRA image (None if it can't be drawn on this PC)."""
    key = (ch, size)
    if key not in _emoji_cache:
        img = None
        try:
            from PIL import Image, ImageDraw, ImageFont
            font = ImageFont.truetype(EMOJI_FONT, 109)          # Segoe UI Emoji's bitmap size
            im = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((80, 80), ch, font=font, embedded_color=True, anchor="mm")
            bbox = im.getbbox()
            if bbox:
                im = im.crop(bbox)
                sc = size / max(im.size)
                im = im.resize((max(1, round(im.width * sc)), max(1, round(im.height * sc))), Image.LANCZOS)
                img = cv2.cvtColor(np.array(im), cv2.COLOR_RGBA2BGRA)
        except Exception:
            img = None
        _emoji_cache[key] = img
    return _emoji_cache[key]


def paste(dst, src, cx, cy, scale=1.0, alpha=1.0):
    """Alpha-blend a BGRA image centred at (cx, cy)."""
    if src is None or scale <= 0.02 or alpha <= 0.01: return
    if abs(scale - 1) > 0.01:
        src = cv2.resize(src, (max(1, int(src.shape[1] * scale)), max(1, int(src.shape[0] * scale))),
                         interpolation=cv2.INTER_LINEAR)
    h, w = src.shape[:2]
    x0, y0 = int(cx - w / 2), int(cy - h / 2)
    X0, Y0, X1, Y1 = max(0, x0), max(0, y0), min(dst.shape[1], x0 + w), min(dst.shape[0], y0 + h)
    if X1 <= X0 or Y1 <= Y0: return
    s = src[Y0 - y0:Y1 - y0, X0 - x0:X1 - x0]
    a = (s[..., 3:4].astype(np.float32) / 255.0) * alpha
    roi = dst[Y0:Y1, X0:X1]
    roi[:] = (roi * (1 - a) + s[..., :3] * a).astype(np.uint8)


def _ease_out_back(x):
    c = 1.70158
    x -= 1
    return 1 + (c + 1) * x ** 3 + c * x ** 2


def _pop(t, t0, dur, rise=0.18, fall=0.15):
    """0 -> overshoot -> 1 -> hold -> 0 envelope for pop-in stickers."""
    u = t - t0
    if u < 0 or u > dur: return 0.0
    if u < rise: return max(0.0, _ease_out_back(u / rise))
    if u > dur - fall: return max(0.0, (dur - u) / fall)
    return 1.0


class Effects:
    """events: [{"t": start s, "end": s, "kind": zoom|circle|shake|flash|emoji, "emoji": str, "color": name}]
    cut_times: output times of jump cuts. region: (y0, y1) rows that zoom/shake act on (split = top panel)."""

    def __init__(self, events, cut_times, fps, size, heavy=True, region=None, colors=None):
        self.ev = sorted(events, key=lambda e: e["t"])
        self.cuts = sorted(cut_times)
        self.fps = fps
        self.ow, self.oh = size
        self.heavy = heavy
        self.region = region
        self.colors = colors or {}
        self.rng = random.Random(7)

    def _active(self, t, kinds, pre=0.0, post=0.0):
        return [e for e in self.ev if e["kind"] in kinds and e["t"] - pre <= t <= e["end"] + post]

    def _seg_index(self, t):
        return sum(1 for c in self.cuts if c <= t)

    def _zoom_shake(self, part, cx0, cy0, t, base, punch):
        """Zoom (alternating slight punch between cuts + eased punch-ins) and shake one panel.
        Returns the new panel (or None if untouched) and the transform for points inside it."""
        ph, pw = part.shape[:2]
        z = 1.0 + (base if self._seg_index(t) % 2 else 0.0)
        for e in self._active(t, ("zoom",), post=0.22):
            u = t - e["t"]
            if u < 0.12: k = _ease_out_back(max(0, u) / 0.12)
            elif t > e["end"]: k = max(0.0, 1 - (t - e["end"]) / 0.22)
            else: k = 1.0
            z = max(z, 1.0 + punch * k)
        dx = dy = 0.0
        for e in self._active(t, ("shake",)):
            amp = 22 * max(0.0, 1 - (t - e["t"]) / max(0.05, e["end"] - e["t"]))
            dx += self.rng.uniform(-amp, amp); dy += self.rng.uniform(-amp, amp)
        if not (z > 1.001 or dx or dy): return None, None
        cx = min(max(cx0, pw / (2 * z)), pw - pw / (2 * z))
        cy = min(max(cy0, ph / (2 * z)), ph - ph / (2 * z))
        M = np.float32([[z, 0, pw / 2 - cx * z + dx], [0, z, ph / 2 - cy * z + dy]])
        out = cv2.warpAffine(part, M, (pw, ph), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        return out, (lambda x, y, s: ((x - cx) * z + pw / 2 + dx, (y - cy) * z + ph / 2 + dy, s * z))

    def apply(self, img, face, t):
        if not self.heavy: return img
        split = self.region is not None
        y0, y1 = self.region or (0, img.shape[0])
        part = img[y0:y1]
        ph, pw = part.shape[:2]
        fx, fy, fs = face if face else (pw / 2, ph * 0.42, pw * 0.15)
        fy -= y0
        # 1) zoom + shake. Split: the facecam is already a tight crop, so it only gets a gentle punch
        #    (a big one turns it to mush); the gameplay below gets the real punch-ins, around its centre.
        new, move = self._zoom_shake(part, fx, fy, t, 0.0 if split else 0.07, 0.08 if split else 0.2)
        if new is not None:
            img[y0:y1] = new                  # composers hand us a fresh frame, so editing in place is safe
            fx, fy, fs = move(fx, fy, fs)
            part = img[y0:y1]
        game = None
        if split and y1 < img.shape[0]:
            game = img[y1:]
            gh, gw = game.shape[:2]
            new, _ = self._zoom_shake(game, gw / 2, gh / 2, t, 0.07, 0.18)
            if new is not None:
                img[y1:] = new; game = img[y1:]
        # 2) red circle + arrow, drawn on around the subject (the face in the facecam)
        for e in self._active(t, ("circle",)):
            self._circle_arrow(part, fx, fy, fs, t, e)
        # 3) emoji pops: next to the face, or over the gameplay in split so the small facecam isn't crowded
        for e in self._active(t, ("emoji",)):
            k = _pop(t, e["t"], e["end"] - e["t"])
            wob = math.sin((t - e["t"]) * 9) * 0.04
            if game is not None:
                gh, gw = game.shape[:2]
                em = emoji_image(e.get("emoji") or "🔥", int(gw * 0.2))
                side = 0.8 if int(e["t"] * 10) % 2 else 0.2       # alternate corners
                paste(game, em, gw * side, gh * 0.22, scale=k * (1 + wob))
            else:
                em = emoji_image(e.get("emoji") or "🔥", int(pw * 0.2))
                ex = min(max(fx + fs * 0.9, pw * 0.15), pw * 0.85)
                ey = min(max(fy + fs * 0.8, ph * 0.15), ph * 0.85)
                paste(part, em, ex, ey, scale=k * (1 + wob))
        # 4) white flash on cuts and flash events
        fl = 0.0
        for c in self.cuts:
            if 0 <= t - c < 0.16: fl = max(fl, 0.75 * (1 - (t - c) / 0.16))
        for e in self._active(t, ("flash",)):
            if 0 <= t - e["t"] < 0.2: fl = max(fl, 0.9 * (1 - (t - e["t"]) / 0.2))
        if fl > 0.01:
            img[:] = cv2.addWeighted(img, 1 - fl, np.full_like(img, 255), fl, 0)
        return img

    def _circle_arrow(self, part, fx, fy, fs, t, e):
        dur = e["end"] - e["t"]
        u = t - e["t"]
        fade = min(1.0, (dur - u) / 0.2) if u > dur - 0.2 else 1.0
        draw = min(1.0, u / 0.25)                    # stroke draws itself on
        rgb = self.colors.get(e.get("color") or "red", (255, 40, 40))
        col = (rgb[2], rgb[1], rgb[0])
        ph, pw = part.shape[:2]
        r = int(min(max(fs * 1.05, pw * 0.09), pw * 0.2, ph * 0.3))
        fx = min(max(fx, r * 1.1), pw - r * 1.1); fy = min(max(fy, r * 1.1), ph - r * 1.1)
        th = max(6, int(pw * 0.011))
        over = part.copy()
        # hand-drawn look: slightly squashed ellipse, tilted, a little past 360 degrees
        axes = (int(r * 1.08), int(r * 0.92))
        end_ang = -90 + 380 * draw
        cv2.ellipse(over, (int(fx), int(fy)), axes, -12, -90, end_ang, (0, 0, 0), th + 6, cv2.LINE_AA)
        cv2.ellipse(over, (int(fx), int(fy)), axes, -12, -90, end_ang, col, th, cv2.LINE_AA)
        if u > 0.18:
            # arrow comes in from the emptier side and points at the circle
            side = -1 if fx > pw / 2 else 1
            a = min(1.0, (u - 0.18) / 0.15)
            tip = (fx + side * r * 0.85, fy - r * 0.85)
            tail_full = (fx + side * (r * 0.85 + pw * 0.25), fy - r * 0.85 - ph * 0.12)
            if tail_full[1] < ph * 0.04:        # no room above: come in from below instead
                tip = (fx + side * r * 0.85, fy + r * 0.85)
                tail_full = (tip[0] + side * pw * 0.25, tip[1] + ph * 0.12)
            tail_full = (min(max(tail_full[0], pw * 0.03), pw * 0.97), min(max(tail_full[1], ph * 0.03), ph * 0.97))
            tail = (tip[0] + (tail_full[0] - tip[0]) * a, tip[1] + (tail_full[1] - tip[1]) * a)
            p1, p0 = tuple(int(v) for v in tail), tuple(int(v) for v in tip)
            cv2.arrowedLine(over, p1, p0, (0, 0, 0), th + 6, cv2.LINE_AA, tipLength=0.3)
            cv2.arrowedLine(over, p1, p0, col, th, cv2.LINE_AA, tipLength=0.3)
        part[:] = cv2.addWeighted(over, fade, part, 1 - fade, 0) if fade < 1 else over


def auto_events(timeline, highlights, focus_out, total):
    """Effects for when the AI gave none: emphasis on highlighted words and on must-show moments."""
    ev, last_zoom, last_circle, n = [], -9.0, -9.0, 0
    for i, st, en in timeline:
        col = highlights.get(i)
        if not col: continue
        n += 1
        if st - last_zoom >= 1.2:
            ev.append(dict(t=st, end=max(en, st + 0.5), kind="zoom")); last_zoom = st
        if col == "red":
            ev.append(dict(t=st, end=st + 0.35, kind="shake"))
        if n % 2 == 0:
            ev.append(dict(t=st, end=st + 1.1, kind="emoji", emoji=COLOR_EMOJI.get(col, "🔥")))
        if st - last_circle >= 4.5 and n % 3 == 1:
            ev.append(dict(t=st, end=min(total, st + 1.5), kind="circle", color="red")); last_circle = st
    for a, b in focus_out:
        ev.append(dict(t=a, end=min(total, a + 1.6), kind="circle", color="red"))
        ev.append(dict(t=a, end=min(b, a + 1.0), kind="zoom"))
    return ev


def clean_events(events, total):
    out = []
    for e in events:
        if e.get("kind") not in KINDS: continue
        t = max(0.0, float(e["t"])); end = min(total, max(float(e.get("end", t)), t + 0.3))
        if t >= total: continue
        out.append(dict(e, t=t, end=end))
    return out


# ---------------------------------------------------------------- meme frame layout
FRAME_W, FRAME_H = 1080, 1920
FRAME_TOP = 420          # y of the square video panel (1080 x 1080)


def framed_wrap(compose):
    """Wrap a 1080x1080 composer into the black 1080x1920 meme frame."""
    canvas = np.zeros((FRAME_H, FRAME_W, 3), np.uint8)

    def wrap(img):
        out = canvas.copy()
        out[FRAME_TOP:FRAME_TOP + img.shape[0], :img.shape[1]] = img
        return out
    return wrap


def frame_title_emoji(img, emoji):
    """The emoji under the frame title (drawn every frame; it's cached)."""
    em = emoji_image(emoji, 86)
    if em is not None: paste(img, em, FRAME_W / 2, 345)
