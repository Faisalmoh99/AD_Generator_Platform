"""Deterministic macro crops of the locked product packshot (STOP 2 fix for macro identity).

Kontext re-framing the full packshot keeps the packshot's framing, and Kontext multi copies the
packshot outright (STOP 1). So a macro shot is cut from the approved packshot in code (the product
pixels are the packshot's own: identity exact by construction) and the reference model only
relights the crop onto black.

The product is found as the pixels that differ from the background, estimated per row from the
left and right image borders (packshots are a centred product on a plain studio backdrop). No
model involved; the same packshot always gives the same crop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image

CropRegion = Literal["cap", "edge"]

_BORDER = 0.06  # fraction of the width sampled on each side as the background
_DIFF = 38.0  # max-channel difference from the background that counts as product
_MIN_SHARE = 0.02  # a row/column is product if this share of its pixels differ


@dataclass(frozen=True, slots=True)
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


def product_mask(rgb: NDArray[np.uint8]) -> NDArray[np.bool_]:
    """Boolean mask of the pixels that differ from the row's border colour."""
    _, w, _ = rgb.shape
    b = max(2, int(w * _BORDER))
    border = np.concatenate([rgb[:, :b, :], rgb[:, w - b :, :]], axis=1)
    bg = np.median(border, axis=1)[:, None, :]
    diff = np.abs(rgb.astype(np.float32) - bg.astype(np.float32)).max(axis=2)
    mask: NDArray[np.bool_] = diff > _DIFF
    return mask


def product_box(mask: NDArray[np.bool_]) -> Box | None:
    h, w = mask.shape
    cols = np.where(mask.mean(axis=0) > _MIN_SHARE)[0]
    rows = np.where(mask.mean(axis=1) > _MIN_SHARE)[0]
    if cols.size == 0 or rows.size == 0:
        return None
    box = Box(int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)
    if box.w < 0.1 * w or box.h < 0.1 * h:
        return None
    return box


def _row_span(mask: NDArray[np.bool_], y: int) -> tuple[int, int] | None:
    xs = np.where(mask[y])[0]
    return (int(xs[0]), int(xs[-1]) + 1) if xs.size else None


def crop_box(mask: NDArray[np.bool_], region: CropRegion, aspect: float) -> Box:
    """The crop rectangle (``aspect`` = width / height of the target frame), inside the image.

    cap: centred on the top of the product, about 1.7x the cap's width, the cap in the upper part
    of the frame with the neck and shoulders below it. edge: the product's upper right corner (the
    shoulder where facets catch the rim light), about half the product's width."""
    h, w = mask.shape
    box = product_box(mask)
    if box is None:  # no product found: the upper middle of the image
        cw = int(w * 0.5)
        return _fit(w // 2, int(h * 0.3), cw, aspect, w, h)
    if region == "cap":
        y = min(box.y1 - 1, box.y0 + max(2, int(0.04 * box.h)))
        span = _row_span(mask, y) or (box.x0, box.x1)
        cap_w = max(span[1] - span[0], int(0.25 * box.w))
        cw = int(cap_w * 1.7)
        ch = int(cw / aspect)
        cy = box.y0 - int(0.12 * ch) + ch // 2
        return _fit((span[0] + span[1]) // 2, cy, cw, aspect, w, h)
    # edge: the upper right shoulder of the body
    cw = max(int(box.w * 0.5), 32)
    cx = box.x1 - int(cw * 0.35)
    cy = box.y0 + int(box.h * 0.42)
    return _fit(cx, cy, cw, aspect, w, h)


def _fit(cx: int, cy: int, cw: int, aspect: float, w: int, h: int) -> Box:
    cw = min(cw, w, int(h * aspect))
    ch = min(round(cw / aspect), h)
    x0 = min(max(0, cx - cw // 2), w - cw)
    y0 = min(max(0, cy - ch // 2), h - ch)
    return Box(x0, y0, x0 + cw, y0 + ch)


def macro_crop(packshot: Path, out: Path, *, region: CropRegion, width: int, height: int) -> Path:
    """Cut ``region`` out of the packshot at the frame's aspect ratio and upscale it (Lanczos) to
    the frame size. Pure function of the input bytes."""
    with Image.open(packshot) as im:
        rgb = np.asarray(im.convert("RGB"))
    b = crop_box(product_mask(rgb), region, width / height)
    img = Image.fromarray(rgb[b.y0 : b.y1, b.x0 : b.x1])
    img = img.resize((width, height), Image.Resampling.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG", optimize=False)
    return out
