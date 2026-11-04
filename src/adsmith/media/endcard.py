"""End card: brand name + tagline as a PNG (then a short clip), Arabic shaped correctly.

ffmpeg ``drawtext`` cannot shape Arabic without libraqm, and this Pillow build has no raqm either,
so Arabic is shaped explicitly: ``arabic_reshaper`` joins the letters into their contextual forms
and ``python-bidi`` reorders each line for right-to-left display. Text is wrapped in LOGICAL order
first and every line is shaped separately (shaping a whole paragraph and wrapping the visual
string would put the first words on the last line).
licence in ``assets/fonts``). The font must contain the Arabic Presentation Forms the reshaper
emits: Tajawal, Cairo and Almarai lack ~56 of them and render boxes ("tofu"); Noto Naskh Arabic
(SIL OFL 1.1, variable weight) covers all of them plus Latin, so it is the bundled font.
``missing_glyphs`` guards against regressions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT = FONT_DIR / "NotoNaskhArabic-Variable.ttf"
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_reshaper = arabic_reshaper.ArabicReshaper(configuration={"delete_harakat": False})


def font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size=size)
    f.set_variation_by_name(weight)
    return f


def missing_glyphs(text: str, f: ImageFont.FreeTypeFont) -> list[str]:
    """Characters of the SHAPED text that the font would draw as the .notdef box."""

    def pixels(ch: str) -> bytes:
        img = Image.new("L", (int(f.size * 2), int(f.size * 2)))
        ImageDraw.Draw(img).text((0, 0), ch, font=f, fill=255)
        return img.tobytes()

    notdef = pixels("\U0010fffd")
    return sorted({ch for ch in shape(text) if not ch.isspace() and pixels(ch) == notdef})


def has_arabic(text: str) -> bool:
    return bool(_ARABIC.search(text))


def shape(line: str) -> str:
    """One logical line → the string Pillow's basic layout should draw left to right."""
    if not has_arabic(line):
        return line
    return str(get_display(_reshaper.reshape(line)))


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word wrap in logical order, measuring the SHAPED width of each candidate line."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and font.getlength(shape(candidate)) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


@dataclass(frozen=True, slots=True)
class EndCardStyle:
    background: tuple[int, int, int] = (14, 11, 9)
    accent: tuple[int, int, int] = (200, 150, 62)
    text: tuple[int, int, int] = (243, 234, 215)


MIN_TEXT_LUMINANCE = 180


def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def style_from_palette(palette: list[str]) -> EndCardStyle:
    """Darkest palette colour as background, the most saturated as accent, the lightest as text."""
    if len(palette) < 2:
        return EndCardStyle()
    rgb = [_hex(c) for c in palette]

    def lum(c: tuple[int, int, int]) -> float:
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def sat(c: tuple[int, int, int]) -> int:
        return max(c) - min(c)

    dark = min(rgb, key=lum)
    light = max(rgb, key=lum)
    accent = max((c for c in rgb if c not in (dark, light)), key=sat, default=light)
    bg = tuple(max(0, int(v * 0.55)) for v in dark)
    # Text must stay readable on the darkened card: a dark-only palette falls back to warm cream.
    text = light if lum(light) >= MIN_TEXT_LUMINANCE else EndCardStyle().text
    return EndCardStyle(background=bg, accent=accent, text=text)  # type: ignore[arg-type]


def render_endcard(
    out: Path,
    *,
    brand: str,
    tagline: str,
    width: int,
    height: int,
    style: EndCardStyle | None = None,
    background_frame: Path | None = None,
    text: bool = True,
) -> Path:
    """Vertical-safe layout: brand centred slightly above the middle, a short accent rule, the
    tagline below. If a background frame is given (the ad's last frame) it is blurred and darkened
    so the card continues the final shot instead of cutting to a flat colour."""
    st = style or EndCardStyle()
    if background_frame is not None and background_frame.exists():
        with Image.open(background_frame) as im:
            bg = im.convert("RGB").resize((width, height))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=width // 40))
        shade = Image.new("RGB", (width, height), st.background)
        img = Image.blend(bg, shade, 0.72)
    else:
        img = Image.new("RGB", (width, height), st.background)
    if not text:  # background plate for the dissolve: no brand, rule or tagline
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return out
    draw = ImageDraw.Draw(img)
    margin = int(width * 0.1)
    max_w = width - 2 * margin

    brand_font = font(int(width * 0.13), "Bold")
    while brand_font.getlength(shape(brand)) > max_w and brand_font.size > width * 0.06:
        brand_font = font(int(brand_font.size * 0.9), "Bold")
    tag_font = font(int(width * 0.052), "Medium")
    tag_lines = wrap(tagline, tag_font, max_w)[:3]
    for content, f in ((brand, brand_font), (tagline, tag_font)):
        if missing := missing_glyphs(content, f):
            raise ValueError(f"end-card font lacks glyphs {missing} for {content!r}")

    def centred(text: str, f: ImageFont.FreeTypeFont, y: int, fill: tuple[int, int, int]) -> int:
        shaped = shape(text)
        left, top, right, bottom = draw.textbbox((0, 0), shaped, font=f)
        draw.text(((width - (right - left)) / 2 - left, y - top), shaped, font=f, fill=fill)
        return int(y + (bottom - top))

    y = int(height * 0.40)
    y = centred(brand, brand_font, y, st.text)
    y += int(height * 0.03)
    rule_w = int(width * 0.16)
    draw.rectangle(
        [((width - rule_w) // 2, y), ((width + rule_w) // 2, y + max(2, height // 400))],
        fill=st.accent,
    )
    y += int(height * 0.035)
    for line in tag_lines:
        y = centred(line, tag_font, y, st.text) + int(tag_font.size * 0.45)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def render_slate(out: Path, *, message: str, width: int, height: int) -> Path:
    """'Scene unavailable' placeholder for partial mode = slate (keeps the ad's timing)."""
    img = Image.new("RGB", (width, height), (18, 18, 20))
    draw = ImageDraw.Draw(img)
    slate_font = font(int(width * 0.05))
    lines = wrap(message, slate_font, int(width * 0.8))
    y = height // 2 - len(lines) * slate_font.size // 2
    for line in lines:
        shaped = shape(line)
        left, top, right, _ = draw.textbbox((0, 0), shaped, font=slate_font)
        x = (width - (right - left)) / 2 - left
        draw.text((x, y - top), shaped, font=slate_font, fill=(150, 150, 155))
        y += int(slate_font.size * 1.5)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
