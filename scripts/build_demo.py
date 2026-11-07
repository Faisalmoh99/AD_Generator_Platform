"""Build the meeting demo videos from stored media only (local, $0; ffmpeg + Pillow).

    uv run python scripts/build_demo.py --studio rec.webm --marks marks.json

demo.mp4 (about 2 min): a title card, the studio walkthrough of the perfume run, the perfume ad,
the coffee ad, try 2 (human prompt), and both side-by-sides with Halai. demo_short.mp4 (about
34 s): the title, the perfume ad, the coffee ad and the perfume side-by-side held 14 s.
1920x1080, 30 fps. Arabic captions are shaped and drawn with the end card's font (Noto Naskh
Arabic); each ad keeps its own sound at -16 LUFS (gain plus a -1.5 dBTP limiter); the other
sections are silent.

``marks.json``: [[label, seconds], ...] from the recording script (when each section is on
screen); the captions follow them.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from adsmith.media.endcard import font, shape

W, H, FPS = 1920, 1080, 30
BAND = 140  # caption band at the bottom
BG = (18, 18, 20)
GOLD = (232, 196, 120)
INK = (240, 236, 228)
MEDIA = Path("docs/media")
LUFS = -16.0

# Studio sections: the label the recording marks, and the caption shown from that mark.
STUDIO_CAPTIONS = [
    ("request", "الطلب وصورة المنتج: «اعلان لعطر رجالي»"),
    ("insights", "حقائق إنسانية من بنك الثقافة السعودية"),
    ("sketches", "أدوات بصرية لا يصنعها إلا الفيديو، تُكتب قبل الأفكار"),
    ("concepts", "ثلاث أفكار مختلفة، وسبعة معايير منها السحر البصري"),
    ("pick", "الكود يختار الفكرة من الدرجات: «ريحتك سبقتك»"),
    ("spend gate", "بوابة الصرف: لا يُصرف ريال قبل موافقة الإنسان"),
    ("director", "المخرج الإبداعي يراجع كل قاعدة ويعيد الكتابة"),
    ("copy", "اسم العلامة والشعار بلهجة سعودية بيضاء"),
    ("prompt", "أمر Seedance النهائي، قصير والحدث المهم أولاً"),
    ("final", "الإعلان النهائي"),
]


def run(*args: str) -> str:
    out = subprocess.run(args, capture_output=True, text=True, check=True)
    return out.stdout + out.stderr


def caption_png(text: str, path: Path, *, size: int = 46) -> Path:
    img = Image.new("RGBA", (W, BAND), (*BG, 235))
    d = ImageDraw.Draw(img)
    f = font(size, "Medium")
    line = shape(text)
    w = d.textlength(line, font=f)
    d.text(((W - w) / 2, (BAND - size * 1.5) / 2), line, font=f, fill=INK)
    img.save(path)
    return path


def title_png(path: Path) -> Path:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for text, size, color, y in (
        ("طبقة إبداعية فوق Seedance", 96, GOLD, 360),
        ("فكرة تقف على حقيقة إنسانية، وصورة لا يصنعها إلا الفيديو", 48, INK, 520),
        ("من طلب واحد إلى إعلان: الفكرة، الأمر، النص، والفيديو", 40, INK, 600),
        ("Adsmith · 2026-11-07", 32, (150, 150, 150), 760),
    ):
        f = font(size, "Bold" if size > 60 else "Regular")
        line = shape(text)
        w = d.textlength(line, font=f)
        d.text(((W - w) / 2, y), line, font=f, fill=color)
    img.save(path)
    return path


def _integrated(src: Path, afilter: str | None = None) -> float:
    af = f"{afilter}," if afilter else ""
    text = run("ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn", "-af",
               f"{af}ebur128=framelog=quiet", "-f", "null", "-")  # fmt: skip
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", text)[-1])


def loudnorm(src: Path) -> str:
    """To -16 LUFS: a gain, then a limiter at -1.5 dBTP, corrected until within 0.3 LU (the
    limiter shaves peaks and lowers the result). ffmpeg's two-pass loudnorm falls back to dynamic
    mode and overshoots when the loudness range or the peaks forbid a linear gain (try 2: LRA
    17.8 LU)."""
    limit = f"alimiter=limit={10 ** (-1.5 / 20):.3f}:level=false"
    gain = LUFS - _integrated(src)
    for _ in range(4):
        got = _integrated(src, f"volume={gain:.2f}dB,{limit}")
        if abs(got - LUFS) <= 0.3:
            break
        gain += LUFS - got
    return f"volume={gain:.2f}dB,{limit}"


def fit(area_h: int) -> str:
    """Scale into the picture area above the caption band and pad on the dark background."""
    return (
        f"scale={W}:{area_h}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{area_h}:(ow-iw)/2:(oh-ih)/2:color=0x121214,"
        f"pad={W}:{H}:0:0:color=0x121214,fps={FPS},format=yuv420p"
    )


def segment(
    out: Path, src: Path, captions: list[tuple[float, float, Path]], *, seconds: float,
    start: float = 0.0, still: bool = False, sound: bool = False,
) -> Path:  # fmt: skip
    """One section as a uniform clip: video fitted above the band, captions overlaid in their
    windows, its own sound normalised or a silent track."""
    args = ["ffmpeg", "-v", "error", "-y"]
    args += ["-loop", "1", "-t", f"{seconds}", "-i", str(src)] if still else [
        "-ss", f"{start}", "-t", f"{seconds}", "-i", str(src)]  # fmt: skip
    for _, _, png in captions:
        args += ["-i", str(png)]
    if not sound:
        args += ["-f", "lavfi", "-t", f"{seconds}", "-i", "anullsrc=r=48000:cl=stereo"]
    chain = f"[0:v]{fit(H - BAND)}[v0]"
    last = "v0"
    for i, (a, b, _) in enumerate(captions, start=1):
        chain += f";[{last}][{i}:v]overlay=0:{H - BAND}:enable='between(t,{a},{b})'[v{i}]"
        last = f"v{i}"
    audio_in = "0:a" if sound else f"{len(captions) + 1}:a"
    afilter = (loudnorm(src) + ",") if sound else ""
    chain += f";[{audio_in}]{afilter}aresample=48000,aformat=channel_layouts=stereo[a]"
    args += ["-filter_complex", chain, "-map", f"[{last}]", "-map", "[a]", "-t", f"{seconds}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
             str(out)]  # fmt: skip
    subprocess.run(args, check=True)
    return out


def duration(path: Path) -> float:
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                     "csv=p=0", str(path)).strip())  # fmt: skip


def concat(parts: list[Path], out: Path, work: Path) -> Path:
    lst = work / f"{out.stem}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", "-movflags", "+faststart", str(out)], check=True)  # fmt: skip
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--studio", type=Path, required=True)
    ap.add_argument("--marks", type=Path, required=True)
    ap.add_argument("--studio-start", type=float, default=2.0)
    args = ap.parse_args()
    marks = dict(json.loads(args.marks.read_text()))
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)

        def cap(name: str, text: str) -> Path:
            return caption_png(text, work / f"{name}.png")

        def ad(name: str, src: Path, text: str) -> Path:
            s = duration(src)
            return segment(work / f"{name}.mp4", src, [(0, s, cap(name, text))], seconds=s,
                           sound=True)  # fmt: skip

        def board(name: str, src: Path, text: str, s: float = 6.0) -> Path:
            return segment(work / f"{name}.mp4", src, [(0, s, cap(name, text))], seconds=s,
                           still=True)  # fmt: skip

        title = segment(work / "title.mp4", title_png(work / "title.png"), [], seconds=4.0,
                        still=True)  # fmt: skip
        t0, end = args.studio_start, marks["end"]
        studio_caps = []
        for i, (label, text) in enumerate(STUDIO_CAPTIONS):
            a = max(0.0, marks[label] - t0)
            nxt = STUDIO_CAPTIONS[i + 1][0] if i + 1 < len(STUDIO_CAPTIONS) else "end"
            b = marks[nxt] - t0
            studio_caps.append((a, b, cap(f"studio_{i}", text)))
        studio = segment(work / "studio.mp4", args.studio, studio_caps, seconds=end - t0,
                         start=t0)  # fmt: skip
        perfume = ad("perfume", MEDIA / "perfume_system_alone.mp4",
                     "عطر رجالي، من النظام وحده · 0.35$ للفيديو · المحكّم 6.85 من 10")  # fmt: skip
        coffee = ad("coffee", MEDIA / "coffee_system_alone.mp4",
                    "قهوة سعودية مختصة، من النظام وحده · 0.35$ · المحكّم 5.8 من 10")  # fmt: skip
        try2 = ad("try2", MEDIA / "try2_human_prompt.mp4",
                  "المحاولة الثانية: أمر كتبه إنسان · 2.44$ · المحكّم 6.0 من 10")  # fmt: skip
        vs = "عطرنا مقابل إعلان حلائي: لدينا فكرة وصورة، ولديهم المنتج في اليد طوال الوقت"
        sbs_p = board("sbs_perfume", MEDIA / "perfume_vs_halai.png", vs)
        sbs_c = board("sbs_coffee", MEDIA / "coffee_vs_halai.png",
                      "قهوتنا مقابل إعلان حلائي")  # fmt: skip
        full = concat([title, studio, perfume, coffee, try2, sbs_p, sbs_c],
                      MEDIA / "demo.mp4", work)  # fmt: skip
        vs_short = "عطرنا مقابل إعلان حلائي: لدينا فكرة وصورة لا يصنعها إلا الفيديو"
        sbs_short = board("sbs_short", MEDIA / "perfume_vs_halai.png", vs_short, 14.0)
        short = concat([title, perfume, coffee, sbs_short], MEDIA / "demo_short.mp4", work)
        for p in (full, short):
            print(f"{p}: {duration(p):.1f} s")


if __name__ == "__main__":
    main()
