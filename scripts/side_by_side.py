"""Two ads side by side as one image: N frames sampled evenly from each, one row per ad, with the
time of each frame (local, $0; ffmpeg only).

    uv run python scripts/side_by_side.py data/runs/<run>/final.mp4 path/to/halai.mp4 \
        --labels "Adsmith (system alone)" "Halai" -o docs/media/coffee_vs_halai.png
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from adsmith.media.endcard import font

TILE_H, PAD, LABEL_W = 320, 8, 220
BG, INK = (18, 18, 20), (235, 235, 235)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return float(out.strip())


def frames(path: Path, n: int, work: Path) -> list[tuple[float, Image.Image]]:
    total = duration(path)
    out = []
    for i in range(n):
        t = total * (i + 0.5) / n
        png = work / f"{path.stem}_{i}.png"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
             str(png)],
            check=True,
        )  # fmt: skip
        img = Image.open(png).convert("RGB")
        img = img.resize((round(img.width * TILE_H / img.height), TILE_H))
        out.append((t, img))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("videos", nargs=2, type=Path)
    ap.add_argument("--labels", nargs=2, default=["ours", "theirs"])
    ap.add_argument("-n", type=int, default=8, help="frames per ad")
    ap.add_argument("-o", "--out", type=Path, default=Path("side_by_side.png"))
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        rows = [frames(v, args.n, Path(tmp)) for v in args.videos]
    width = LABEL_W + max(sum(im.width + PAD for _, im in row) for row in rows) + PAD
    height = len(rows) * (TILE_H + 30 + PAD) + PAD
    sheet = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(sheet)
    y = PAD
    for label, row in zip(args.labels, rows, strict=True):
        draw.text((PAD, y + TILE_H // 2), label, fill=INK, font=font(20))
        x = LABEL_W
        for t, im in row:
            sheet.paste(im, (x, y))
            draw.text((x, y + TILE_H + 4), f"{t:.1f} s", fill=INK, font=font(16))
            x += im.width + PAD
        y += TILE_H + 30 + PAD
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out)
    print(args.out)


if __name__ == "__main__":
    main()
