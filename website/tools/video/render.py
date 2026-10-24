"""Render the site's clips from the demo's real responses.

    tools/.venv/bin/python tools/video/render.py [model-to-api roles guardrails]

Each clip is drawn frame by frame with Pillow (Python highlighted by Pygments)
from src/data/demo.json, which tools/demo/capture.py records from a running
TurboDRF project, and the code is the demo's own source. Frames are piped to
ffmpeg (H.264 MP4), transcoded to VP9 WebM, and a settled frame is saved as
the WebP poster: public/video/<name>.{mp4,webm,webp}.

Colours match src/styles/shiki-turbo.json and the site's theme.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pygments import lex
from pygments.lexers import PythonLexer
from pygments.token import Token

HERE = Path(__file__).resolve().parent
SITE = HERE.parent.parent
DEMO = json.loads((SITE / "src/data/demo.json").read_text())
CALLS = DEMO["calls"]
OUT = SITE / "public" / "video"
FONTS = HERE / "fonts"

W, H, FPS = 1920, 1080, 30

BG = (10, 13, 14)
PANEL = (12, 16, 18)
PANEL_2 = (17, 22, 24)
LINE = (34, 42, 45)
INK = (227, 235, 231)
INK2 = (150, 166, 159)
INK3 = (96, 112, 106)
MINT = (62, 230, 160)
CYAN = (56, 214, 240)
RED = (251, 113, 133)
AMBER = (244, 201, 122)
LAVENDER = (182, 155, 255)
BLUE = (147, 197, 253)

TOK = {
    "kw": (110, 231, 183),
    "str": (244, 201, 122),
    "num": (251, 159, 137),
    "fn": (143, 184, 255),
    "cls": (199, 166, 255),
    "deco": (240, 166, 232),
    "builtin": (103, 215, 234),
    "com": (93, 112, 104),
    "punct": (142, 163, 154),
    "op": (169, 191, 181),
    "text": (215, 227, 221),
    "key": (143, 227, 192),
}
METHOD = {
    "GET": ((62, 230, 160), (16, 44, 34)),
    "POST": (BLUE, (20, 32, 50)),
    "PUT": (AMBER, (46, 38, 20)),
    "PATCH": (AMBER, (46, 38, 20)),
    "DELETE": ((253, 164, 175), (52, 22, 28)),
}
PEOPLE = {
    "alice": ("admin", "Acme", MINT),
    "maya": ("manager", "Acme", CYAN),
    "victor": ("viewer", "Acme", AMBER),
    "bob": ("admin", "Globex", LAVENDER),
}


# ---------------------------------------------------------------- helpers


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def seg(t, a, b):
    """Progress of t through [a, b], 0..1."""
    return clamp((t - a) / (b - a)) if b > a else float(t >= a)


def ease(x):
    return 1 - (1 - x) ** 3


def ease_back(x):
    c = 1.7
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def mix(a, b, x):
    return tuple(int(round(a[i] + (b[i] - a[i]) * x)) for i in range(3))


def rgba(color, alpha=1.0):
    return (*color[:3], int(round(255 * clamp(alpha))))


@lru_cache(None)
def mono(size, bold=False):
    name = "JetBrainsMono-Bold.ttf" if bold else "JetBrainsMono-Regular.ttf"
    return ImageFont.truetype(str(FONTS / name), size)


@lru_cache(None)
def sans(size, weight=500):
    font = ImageFont.truetype(str(FONTS / "Manrope.ttf"), size)
    font.set_variation_by_axes([weight])
    return font


@lru_cache(None)
def background():
    """Carbon: two soft glows and a faint grid fading out from the top."""
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.zeros((H, W, 3), np.float32) + np.array(BG, np.float32)

    def glow(cx, cy, r, color, k):
        d = np.sqrt(((x - cx) / r) ** 2 + ((y - cy) / (r * 0.8)) ** 2)
        return np.clip(1 - d, 0, 1)[..., None] ** 2 * np.array(color, np.float32) * k

    img += glow(W * 0.85, -H * 0.15, W * 0.75, MINT, 0.20)
    img += glow(-W * 0.05, H * 1.1, W * 0.6, CYAN, 0.13)
    grid = ((x % 64) < 1) | ((y % 64) < 1)
    fade = np.clip(1 - np.sqrt(((x - W / 2) / (W * 0.8)) ** 2 + (y / (H * 0.9)) ** 2), 0, 1)
    img += (grid * fade * 9)[..., None]
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


@lru_cache(None)
def rounded(w, h, r, fill, outline=None, width=1, shadow=False):
    """An antialiased rounded rectangle (drawn at 2x), optionally on a soft shadow."""
    pad = 60 if shadow else 0
    s = 2
    big = Image.new("RGBA", ((w + pad * 2) * s, (h + pad * 2) * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    box = (pad * s, pad * s, (pad + w) * s - 1, (pad + h) * s - 1)
    d.rounded_rectangle(box, r * s, fill=fill, outline=outline, width=width * s if outline else 0)
    img = big.resize((w + pad * 2, h + pad * 2), Image.LANCZOS)
    if not shadow:
        return img
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + 24, pad + w, pad + h + 24), r, fill=(0, 0, 0, 170))
    sh = sh.filter(ImageFilter.GaussianBlur(36))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle((pad, pad, pad + w, pad + h), r, fill=(*MINT, 26))
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    out = Image.alpha_composite(sh, glow)
    return Image.alpha_composite(out, img)


def paste(frame, layer, x, y, alpha=1.0, shadow=False):
    if alpha <= 0:
        return
    if shadow:
        x, y = x - 60, y - 60
    mask = layer.getchannel("A")
    if alpha < 1:
        mask = mask.point(lambda v: int(v * alpha))
    frame.paste(layer, (int(round(x)), int(round(y))), mask)


def panel(frame, x, y, w, h, alpha=1.0, fill=PANEL, r=22, edge=None, shadow=True):
    edge = edge or mix(LINE, MINT, 0.18)
    paste(frame, rounded(w, h, r, rgba(fill), rgba(edge), 1, shadow), x, y, alpha, shadow)


@lru_cache(None)
def chip_img(text, fg, bg, size=17, pad_x=12, h=30, bold=True, font="mono"):
    f = mono(size, bold) if font == "mono" else sans(size, 650)
    tw = int(f.getlength(text))
    w = tw + pad_x * 2
    img = rounded(w, h, 7, rgba(bg))
    img = img.copy()
    d = ImageDraw.Draw(img)
    bbox = f.getbbox("Hg")
    ty = (h - (bbox[3] - bbox[1])) / 2 - bbox[1]
    d.text((pad_x, ty), text, font=f, fill=rgba(fg))
    return img


def chip(frame, x, y, text, fg, bg, alpha=1.0, scale=1.0, **kw):
    img = chip_img(text, fg, bg, **kw)
    if scale != 1.0:
        w, h = img.size
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        x, y = x + (w - img.size[0]) / 2, y + (h - img.size[1]) / 2
    paste(frame, img, x, y, alpha)
    return img.size[0]


def method_chip(frame, x, y, method, alpha=1.0, size=17, h=30):
    fg, bg = METHOD[method]
    img = chip_img(method, fg, bg, size=size, h=h, pad_x=14)
    paste(frame, img, x, y, alpha)
    return img.size[0]


def status_colors(code):
    return ((110, 231, 183), (16, 44, 34)) if code < 300 else ((253, 164, 175), (52, 22, 28))


def avatar(frame, x, y, name, size=48, alpha=1.0):
    color = PEOPLE[name][2]
    img = rounded(size, size, size // 2, rgba(mix(PANEL, color, 0.18)), rgba(mix(PANEL, color, 0.6)), 1)
    img = img.copy()
    d = ImageDraw.Draw(img)
    f = sans(int(size * 0.46), 750)
    letter = name[0].upper()
    tw = f.getlength(letter)
    bbox = f.getbbox(letter)
    d.text(((size - tw) / 2, (size - (bbox[3] - bbox[1])) / 2 - bbox[1]), letter, font=f, fill=rgba(color))
    paste(frame, img, x, y, alpha)


def faded(color, alpha):
    """Text colour at partial opacity. Pillow ignores a text fill's alpha on RGB
    frames, so blend toward the panel colour every text sits on."""
    return mix(PANEL, color, clamp(alpha))


def text(d, x, y, s, font, color, alpha=1.0):
    """Draw a string; Manrope's space is narrow at small sizes, so words get a wider gap."""
    if alpha <= 0.01:
        return
    color, alpha = faded(color, alpha), 1.0
    if font.path.endswith("Manrope.ttf") and " " in s:
        gap = font.getlength(" ") + font.size * 0.14
        for word in s.split(" "):
            d.text((x, y), word, font=font, fill=rgba(color, alpha))
            x += font.getlength(word) + gap
        return
    d.text((x, y), s, font=font, fill=rgba(color, alpha))


# ------------------------------------------------------------ highlighting


def py_tokens(code):
    """Lines of (text, colour) runs for Python source."""
    lines, line = [], []
    toks = list(lex(code, PythonLexer()))
    for i, (tt, val) in enumerate(toks):
        nxt = toks[i + 1][1] if i + 1 < len(toks) else ""
        if tt in Token.Comment:
            c = TOK["com"]
        elif tt in Token.Keyword.Constant:
            c = TOK["num"]
        elif tt in Token.Keyword or tt in Token.Operator.Word:
            c = TOK["kw"]
        elif tt in Token.Name.Builtin.Pseudo:
            c = TOK["kw"]
        elif tt in Token.Name.Builtin:
            c = TOK["builtin"]
        elif tt in Token.Name.Decorator:
            c = TOK["deco"]
        elif tt in Token.Name.Class:
            c = TOK["cls"]
        elif tt in Token.Name.Function or (tt in Token.Name and nxt.startswith("(")):
            c = TOK["fn"]
        elif tt in Token.Literal.String:
            c = TOK["str"]
        elif tt in Token.Literal.Number:
            c = TOK["num"]
        elif tt in Token.Punctuation:
            c = TOK["punct"]
        elif tt in Token.Operator:
            c = TOK["op"]
        else:
            c = TOK["text"]
        parts = val.split("\n")
        for j, p in enumerate(parts):
            if p:
                line.append((p, c))
            if j < len(parts) - 1:
                lines.append(line)
                line = []
    if line:
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def json_tokens(value, indent=2):
    """Lines of (text, colour) runs for JSON, as the API pretty-prints it."""
    lines = []

    def scalar(v):
        if isinstance(v, str):
            return (json.dumps(v), TOK["str"])
        return (json.dumps(v), TOK["num"])

    def walk(v, level, key, last):
        pad = " " * (indent * level)
        comma = [] if last else [(",", TOK["punct"])]
        head = [(pad, TOK["text"])]
        if key is not None:
            head += [(json.dumps(key), TOK["key"]), (": ", TOK["punct"])]
        if isinstance(v, dict) and v:
            lines.append(head + [("{", TOK["punct"])])
            items = list(v.items())
            for i, (k, x) in enumerate(items):
                walk(x, level + 1, k, i == len(items) - 1)
            lines.append([(pad, TOK["text"]), ("}", TOK["punct"])] + comma)
        elif isinstance(v, list) and v:
            lines.append(head + [("[", TOK["punct"])])
            for i, x in enumerate(v):
                walk(x, level + 1, None, i == len(v) - 1)
            lines.append([(pad, TOK["text"]), ("]", TOK["punct"])] + comma)
        else:
            empty = "{}" if isinstance(v, dict) else "[]" if isinstance(v, list) else None
            lines.append(head + ([(empty, TOK["punct"])] if empty else [scalar(v)]) + comma)

    walk(value, 0, None, True)
    return lines


def compact_json(value):
    """One-line JSON runs: {"detail": "..."}."""
    s = json.dumps(value)
    runs, i = [], 0
    import re

    for m in re.finditer(r'"(?:[^"\\]|\\.)*"(\s*:)?|-?\d+(?:\.\d+)?|true|false|null|[{}\[\],:]|\s+|…', s):
        tok = m.group(0)
        if m.group(1):
            runs += [(tok[: -len(m.group(1))], TOK["key"]), (m.group(1), TOK["punct"])]
        elif tok.startswith('"'):
            runs.append((tok, TOK["str"]))
        elif tok[0] in "{}[],:":
            runs.append((tok, TOK["punct"]))
        elif tok.strip() == "":
            runs.append((tok, TOK["text"]))
        else:
            runs.append((tok, TOK["num"]))
        i = m.end()
    return runs


def line_len(line):
    return sum(len(t) for t, _ in line)


def leading(line):
    s = "".join(t for t, _ in line)
    return len(s) - len(s.lstrip(" "))


def draw_runs(d, x, y, runs, font, cw, alpha=1.0, limit=None):
    """Draw coloured runs on a monospace grid, at most `limit` characters."""
    col = 0
    if alpha <= 0.01:
        return sum(len(t) for t, _ in runs) if limit is None else min(limit, sum(len(t) for t, _ in runs))
    for t, c in runs:
        if limit is not None:
            if col >= limit:
                break
            t = t[: max(0, limit - col)]
        if t.strip():
            d.text((x + col * cw, y), t, font=font, fill=rgba(faded(c, alpha)))
        col += len(t)
    return col


def typing_budget(lines, t, cps, line_pause=0.05):
    """How many characters of each line are typed after t seconds (indent is free)."""
    out = []
    for line in lines:
        n = line_len(line)
        lead = leading(line)
        typed = n - lead
        need = typed / cps + (line_pause if typed else 0.02)
        if t >= need:
            out.append(n)
            t -= need
        else:
            out.append(lead + int(t * cps) if t > 0 else 0)
            t = 0
            break
    return out


# -------------------------------------------------------------- encoding


def encode(name, frames, duration, poster_at, draw):
    OUT.mkdir(parents=True, exist_ok=True)
    mp4 = OUT / f"{name}.mp4"
    n = int(round(duration * FPS))
    ff = subprocess.Popen(
        ["nice", "-n", "10", "ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "21",
         "-tune", "animation", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-threads", "4", str(mp4)],
        stdin=subprocess.PIPE,
    )
    poster = None
    for i in range(n):
        t = i / FPS
        frame = draw(t)
        if poster is None and t >= poster_at:
            poster = frame.copy()
        ff.stdin.write(frame.convert("RGB").tobytes())
        if i % 60 == 0:
            print(f"  {name}: {i}/{n}", flush=True)
    ff.stdin.close()
    if ff.wait() != 0:
        raise SystemExit(f"ffmpeg failed for {name}")
    subprocess.run(
        ["nice", "-n", "10", "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-c:v", "libvpx-vp9",
         "-b:v", "0", "-crf", "36", "-row-mt", "1", "-threads", "4", "-an", str(OUT / f"{name}.webm")],
        check=True,
        timeout=900,
    )
    poster.convert("RGB").resize((1280, 720), Image.LANCZOS).save(OUT / f"{name}.webp", quality=82, method=6)
    for ext in ("mp4", "webm", "webp"):
        p = OUT / f"{name}.{ext}"
        print(f"  {p.name}: {p.stat().st_size / 1e6:.2f} MB")


# ======================================================= clip: model-to-api


def clip_model_to_api():
    models_src = (SITE / "tools/demo/demo/models.py").read_text()
    code = "from django.db import models\nfrom turbodrf.mixins import TurboDRFMixin\n\n\n" + models_src[
        models_src.index("class Project") :
    ].rstrip()
    urls_src = (SITE / "tools/demo/urls.py").read_text().rstrip()
    model_lines = py_tokens(code)
    url_lines = py_tokens(urls_src)
    resp = CALLS["list_admin_page1"]
    json_lines = json_tokens(resp["response"])

    EX, EY, EW, EH = 56, 56, 1080, 968
    TX, TY, TW, TH = 1168, 56, 696, 968
    font, cw, lh = mono(21), mono(21).getlength("M"), 34
    tfont, tcw, tlh = mono(18), mono(18).getlength("M"), 27
    code_top = EY + 56 + 26
    visible = int((EH - 56 - 40) / lh)

    T_MODELS = 0.5
    t_models_end = T_MODELS + sum(
        (line_len(l) - leading(l)) / 150 + (0.05 if line_len(l) - leading(l) else 0.02) for l in model_lines
    )
    T_URLS = t_models_end + 0.35
    t_urls_end = T_URLS + sum((line_len(l) - leading(l)) / 150 + 0.05 for l in url_lines)
    T_RUN = t_urls_end + 0.3
    T_CURL = T_RUN + 1.2
    T_RESP = T_CURL + 1.55
    T_LIT = T_RESP + len(json_lines) / 30 + 0.2
    END = T_LIT + 4.2
    FADE = 0.7

    term_cols = int((TW - 52) / tcw)
    cmd_run = [("$ ", MINT), ("python manage.py runserver", INK)]
    served = [("Starting development server at http://127.0.0.1:8000/", INK3)]
    curl1 = [("$ ", MINT), ("curl ", INK), ("localhost:8000/api/projects/?page_size=1", INK), (" \\", INK2)]
    curl2 = [("    -H ", INK2), ('"Authorization: Bearer $ALICE_TOKEN"', TOK["str"])]

    def wrap(line):
        """Terminal soft-wrap at term_cols."""
        out, cur, n = [], [], 0
        for t, c in line:
            while t:
                room = term_cols - n
                piece, t = t[:room], t[room:]
                cur.append((piece, c))
                n += len(piece)
                if n >= term_cols and t:
                    out.append(cur)
                    cur, n = [], 0
        out.append(cur)
        return out

    def draw(t):
        frame = background().copy()
        fade = 1 - seg(t, END, END + FADE)
        panel(frame, EX, EY, EW, EH)
        panel(frame, TX, TY, TW, TH)
        d = ImageDraw.Draw(frame, "RGBA")

        # Editor tabs.
        on_urls = T_URLS <= t < T_RUN + 0.6
        tx = EX + 22
        for label, active in (("models.py", not on_urls), ("urls.py", on_urls)):
            f = sans(19, 600)
            w = int(f.getlength(label)) + 36
            if active:
                paste(frame, rounded(w, 38, 9, rgba(PANEL_2), rgba(LINE)), tx, EY + 10)
                d.rectangle((tx + 12, EY + 46, tx + w - 12, EY + 47), fill=rgba(MINT, 0.9))
            text(d, tx + 18, EY + 17, label, f, INK if active else INK3)
            tx += w + 8
        d.line((EX + 1, EY + 56, EX + EW - 2, EY + 56), fill=rgba(LINE), width=1)
        text(d, TX + 24, TY + 17, "zsh — demo", sans(19, 600), INK3)
        d.line((TX + 1, TY + 56, TX + TW - 2, TY + 56), fill=rgba(LINE), width=1)

        # Editor body.
        lines = url_lines if on_urls else model_lines
        budget = typing_budget(lines, t - (T_URLS if on_urls else T_MODELS), 150)
        if not on_urls and t >= T_URLS:
            budget = [line_len(l) for l in lines]
        cur_line = max(0, len(budget) - 1)
        partial = 0
        if budget and lines:
            n = line_len(lines[cur_line])
            partial = (budget[-1] - leading(lines[cur_line])) / max(1, n - leading(lines[cur_line]))
        scroll = 0.0 if on_urls else clamp(cur_line + partial - (visible - 5), 0, max(0, len(lines) - visible + 1))
        if t >= t_models_end and not on_urls:  # typed, or back from urls.py: show the config
            scroll = clamp(len(lines) - (visible - 4), 0, max(0, len(lines) - visible + 1))
        for i, line in enumerate(lines):
            y = code_top + (i - scroll) * lh
            if y < EY + 60 or y > EY + EH - lh:
                continue
            if i < len(budget):
                text(d, EX + 24, y, f"{i + 1:>2}", font, INK3, 0.6 * fade)
                draw_runs(d, EX + 84, y, line, font, cw, fade, budget[i])
        # Cursor.
        typing = budget and (budget[-1] < line_len(lines[cur_line]) or len(budget) < len(lines))
        if fade > 0 and budget and (typing or int(t * 2.2) % 2 == 0) and t < T_RUN:
            cy = code_top + (cur_line - scroll) * lh
            cx = EX + 84 + (budget[-1] if budget else 0) * cw
            if EY + 60 < cy < EY + EH - lh:
                d.rectangle((cx, cy + 2, cx + 2, cy + 27), fill=rgba(MINT, fade))

        # Terminal body.
        y = TY + 80
        tl = []
        if t >= T_RUN:
            n = int((t - T_RUN) * 45)
            tl.append((cmd_run, n))
        if t >= T_RUN + 0.85:
            tl.append((served, None))
        if t >= T_CURL:
            n = int((t - T_CURL) * 70)
            tl.append(([], None))
            tl.append((curl1, n))
            n2 = n - line_len(curl1)
            if n2 > 0:
                tl.append((curl2, n2))
        if t >= T_RESP:
            shown = int((t - T_RESP) * 30)
            for line in json_lines[:shown]:
                tl.append((line, None))
        lit = seg(t, T_LIT, T_LIT + 0.6)
        for runs, limit in tl:
            for piece in wrap(runs):
                if lit and any('"budget"' in r for r, _ in piece):
                    d.rectangle((TX + 14, y - 3, TX + TW - 14, y + tlh - 4), fill=rgba(MINT, 0.12 * lit * fade))
                    d.rectangle((TX + 14, y - 3, TX + 16, y + tlh - 4), fill=rgba(MINT, lit * fade))
                used = draw_runs(d, TX + 26, y, piece, tfont, tcw, fade, limit)
                if limit is not None:
                    limit -= used
                y += tlh
        # Response chip.
        if t >= T_RESP:
            a = seg(t, T_RESP, T_RESP + 0.3) * fade
            s = 0.6 + 0.4 * ease_back(seg(t, T_RESP, T_RESP + 0.45))
            fg, bg = status_colors(resp["status"])
            label = f"200 OK · {resp['queries']} SQL queries"
            w = chip_img(label, fg, bg, size=16, h=30)
            chip(frame, TX + TW - w.size[0] - 20, TY + 13, label, fg, bg, a, s, size=16, h=30)
        return frame

    return draw, END + FADE + 0.1, T_LIT + 2.5


# ============================================================== clip: roles


def clip_roles():
    url = "/api/projects/1/"
    cols = [("alice", CALLS["detail_admin"]), ("maya", CALLS["detail_manager"]), ("victor", CALLS["detail_viewer"])]
    other = CALLS["detail_other_tenant"]
    font, cw, lh = mono(20), mono(20).getlength("M"), 31
    CW, CH, CY = 576, 556, 196
    xs = [64, 672, 1280]
    T_CARDS, T_JSON = 0.6, 1.5
    T_LIT = T_JSON + 12 * 0.2 + 0.2
    T_NOTE = T_LIT + 0.5
    T_BOB = T_NOTE + 1.3
    END = T_BOB + 5.4
    FADE = 0.7

    def draw(t):
        frame = background().copy()
        d = ImageDraw.Draw(frame, "RGBA")
        fade = 1 - seg(t, END, END + FADE)
        a0 = ease(seg(t, 0.0, 0.5)) * fade
        w = method_chip(frame, 64, 70, "GET", a0, size=22, h=40)
        text(d, 64 + w + 18, 72, url, mono(30), INK, a0)
        text(d, 64, 128, "The same request from four users of the demo project.", sans(25, 500), INK2, a0)

        for i, (name, call) in enumerate(cols):
            p = ease(seg(t, T_CARDS + i * 0.12, T_CARDS + i * 0.12 + 0.6))
            x, y = xs[i], CY + (1 - p) * 40
            a = p * fade
            panel(frame, x, y, CW, CH, a)
            avatar(frame, x + 26, y + 24, name, 50, a)
            role, ws, color = PEOPLE[name]
            text(d, x + 92, y + 26, name, sans(26, 700), INK, a)
            chip(frame, x + 92, y + 60, f"{role} · {ws}", mix(PANEL, color, 0.95), mix(PANEL, color, 0.16), a,
                 size=15, h=26, bold=False, font="sans")
            fg, bg = status_colors(call["status"])
            sa = seg(t, T_JSON, T_JSON + 0.3) * fade
            chip(frame, x + CW - 98, y + 30, f"{call['status']} OK", fg, bg, sa, size=16, h=30)
            d.line((x + 1, y + 102, x + CW - 2, y + 102), fill=rgba(LINE, a), width=1)
            lines = json_tokens(call["response"])
            shown = int((t - T_JSON) / 0.2) + 1 if t >= T_JSON else 0
            lit = seg(t, T_LIT, T_LIT + 0.5)
            for j, line in enumerate(lines[:shown]):
                ly = y + 122 + j * lh
                la = seg(t, T_JSON + j * 0.2, T_JSON + j * 0.2 + 0.25) * fade
                if lit and any('"budget"' in r for r, _ in line):
                    d.rectangle((x + 14, ly - 4, x + CW - 14, ly + lh - 6), fill=rgba(MINT, 0.12 * lit * fade))
                    d.rectangle((x + 14, ly - 4, x + 16, ly + lh - 6), fill=rgba(MINT, lit * fade))
                draw_runs(d, x + 28, ly, line, font, cw, la)
            if name == "victor":
                na = seg(t, T_NOTE, T_NOTE + 0.3) * fade
                s = 0.7 + 0.3 * ease_back(seg(t, T_NOTE, T_NOTE + 0.45))
                ny = y + 122 + len(lines) * lh + 14
                chip(frame, x + 28, ny, "budget left out: viewer doesn't have budget.read", (253, 164, 175),
                     (52, 22, 28), na, s, size=14, h=30, bold=False)
            else:
                na = seg(t, T_NOTE, T_NOTE + 0.3) * fade
                ny = y + 122 + len(lines) * lh + 14
                text(d, x + 28, ny + 4, "budget included: role has budget.read", sans(18, 500), INK3, na)

        # Bob, in the other workspace.
        p = ease(seg(t, T_BOB, T_BOB + 0.6))
        bx, by, bw, bh = 64, 784 + (1 - p) * 40, 1792, 232
        a = p * fade
        panel(frame, bx, by, bw, bh, a, edge=mix(LINE, RED, 0.35))
        avatar(frame, bx + 26, by + 28, "bob", 50, a)
        text(d, bx + 92, by + 30, "bob", sans(26, 700), INK, a)
        chip(frame, bx + 92, by + 64, "admin · Globex", mix(PANEL, LAVENDER, 0.95), mix(PANEL, LAVENDER, 0.16), a,
             size=15, h=26, bold=False, font="sans")
        ra = seg(t, T_BOB + 0.5, T_BOB + 0.8) * fade
        w = method_chip(frame, bx + 360, by + 34, "GET", ra, size=18, h=34)
        text(d, bx + 360 + w + 16, by + 36, url, mono(24), INK, ra)
        sa = seg(t, T_BOB + 1.1, T_BOB + 1.35) * fade
        s = 0.6 + 0.4 * ease_back(seg(t, T_BOB + 1.1, T_BOB + 1.55))
        fg, bg = status_colors(other["status"])
        chip(frame, bx + 360, by + 100, "404 Not Found", fg, bg, sa, s, size=20, h=40)
        ja = seg(t, T_BOB + 1.5, T_BOB + 1.9) * fade
        draw_runs(d, bx + 560, by + 106, compact_json(other["response"]), mono(22), mono(22).getlength("M"), ja)
        na = seg(t, T_BOB + 2.1, T_BOB + 2.5) * fade
        text(d, bx + 360, by + 168, "To a Globex user, Acme's projects don't exist, even for an admin. The answer is 404, not 403.",
             sans(21, 500), INK2, na)
        return frame

    return draw, END + FADE + 0.1, T_BOB + 3.6


# ========================================================= clip: guardrails


def clip_guardrails():
    budget_resp = {k: CALLS["patch_manager_budget"]["response"][k] for k in ("id", "title", "budget")}
    rows = [
        ("bob", CALLS["detail_other_tenant"], "Read another workspace's project"),
        ("alice", CALLS["create_cross_tenant_fk"], "Use another workspace's client"),
        ("victor", CALLS["patch_viewer"], "Edit without update rights"),
        ("maya", dict(CALLS["patch_manager_budget"], response=budget_resp), "Change a field she can't write"),
        ("victor", CALLS["fields_viewer"], "Ask for a hidden field"),
        ("maya", CALLS["delete_manager"], "Delete without delete rights"),
    ]
    notes = {3: "budget unchanged", 4: "only title comes back"}
    RX, RW, RH, GAP, TOP = 64, 1792, 124, 16, 200
    T0, STEP = 0.9, 1.9
    END = T0 + STEP * len(rows) + 2.6
    FADE = 0.7
    mfont, mcw = mono(21), mono(21).getlength("M")
    sfont, scw = mono(18), mono(18).getlength("M")
    STATUS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}

    def short(call):
        r = call["response"]
        if isinstance(r, dict) and "pagination" in r:
            return {"data": r["data"]}
        return r

    def draw(t):
        frame = background().copy()
        d = ImageDraw.Draw(frame, "RGBA")
        fade = 1 - seg(t, END, END + FADE)
        a0 = ease(seg(t, 0, 0.5)) * fade
        text(d, RX, 64, "Requests the demo API refused or trimmed", sans(48, 700), INK, a0)
        text(d, RX, 132, "Each row is a real request and the response the API sent back.", sans(24, 500), INK2, a0)

        for i, (name, call, title) in enumerate(rows):
            t0 = T0 + i * STEP
            p = ease(seg(t, t0, t0 + 0.5))
            if p <= 0:
                continue
            x, y = RX + (1 - p) * 60, TOP + i * (RH + GAP)
            a = p * fade
            ok = call["status"] < 300
            panel(frame, x, y, RW, RH, a, r=18, edge=mix(LINE, MINT if ok else RED, 0.25), shadow=False)
            avatar(frame, x + 24, y + 22, name, 44, a)
            role, ws, color = PEOPLE[name]
            text(d, x + 82, y + 22, name, sans(22, 700), INK, a)
            text(d, x + 82, y + 52, f"{role} · {ws}", sans(17, 500), INK3, a)
            text(d, x + 82, y + 82, title, sans(17, 600), mix(INK2, color, 0.35), a)

            # Request.
            ra = seg(t, t0 + 0.25, t0 + 0.5) * fade
            mx = x + 400
            w = method_chip(frame, mx, y + 22, call["method"], ra, size=16, h=30)
            n = int((t - t0 - 0.3) * 60) if t > t0 + 0.3 else 0
            draw_runs(d, mx + w + 14, y + 24, [(call["url"], INK)], mfont, mcw, ra, n)
            if call["body"]:
                ba = seg(t, t0 + 0.55, t0 + 0.8) * fade
                draw_runs(d, mx, y + 72, compact_json(call["body"]), sfont, scw, ba)

            # Response.
            sx = x + 1060
            sa = seg(t, t0 + 0.95, t0 + 1.15) * fade
            s = 0.55 + 0.45 * ease_back(seg(t, t0 + 0.95, t0 + 1.4))
            fg, bg = status_colors(call["status"])
            label = f"{call['status']} {STATUS[call['status']]}"
            chip(frame, sx, y + 20, label, fg, bg, sa, s, size=18, h=34)
            if i in notes:
                lw = chip_img(label, fg, bg, size=18, h=34).size[0]
                text(d, sx + lw + 14, y + 26, notes[i], sans(18, 600), AMBER, sa)
            ja = seg(t, t0 + 1.2, t0 + 1.5) * fade
            runs = compact_json(short(call))
            if i == 3:  # a few fields of the response, marked as trimmed
                runs = runs[:-1] + [(", ", TOK["punct"]), ("…", INK3), ("}", TOK["punct"])]
            # Keep the response on one line inside the row.
            max_cols = int((x + RW - 28 - sx) / scw)
            if sum(len(r) for r, _ in runs) > max_cols:
                kept, n = [], 0
                for r, c in runs:
                    if n + len(r) > max_cols - 1:
                        kept.append((r[: max(0, max_cols - 1 - n)] + "…", c))
                        break
                    kept.append((r, c))
                    n += len(r)
                runs = kept
            draw_runs(d, sx, y + 74, runs, sfont, scw, ja)
        return frame

    return draw, END + FADE + 0.1, T0 + STEP * len(rows) + 1.2


CLIPS = {"model-to-api": clip_model_to_api, "roles": clip_roles, "guardrails": clip_guardrails}


def main(names):
    for name in names or CLIPS:
        draw, duration, poster_at = CLIPS[name]()
        print(f"{name}: {duration:.1f}s")
        if "--still" in sys.argv:
            OUT.mkdir(parents=True, exist_ok=True)
            draw(poster_at).convert("RGB").save(HERE / f"still-{name}.png")
            continue
        encode(name, None, duration, poster_at, draw)


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if not a.startswith("--")])
