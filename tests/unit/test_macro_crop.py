"""Macro shots cut from the locked packshot in code (STOP 2): identity exact by construction."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from adsmith.domain.vocabulary import ShotVocab
from adsmith.media.crop import crop_box, macro_crop, product_box, product_mask

W, H = 540, 960
BODY = (150, 330, 390, 840)  # x0, y0, x1, y1
CAP = (210, 200, 330, 330)


def _packshot(path: Path) -> Path:
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[:, :] = (236, 222, 196)  # beige studio backdrop
    img[BODY[1] : BODY[3], BODY[0] : BODY[2]] = (30, 60, 70)  # dark glass body
    img[CAP[1] : CAP[3], CAP[0] : CAP[2]] = (12, 12, 14)  # black cap
    Image.fromarray(img).save(path)
    return path


def test_the_product_is_found_on_a_plain_backdrop(tmp_path: Path) -> None:
    rgb = np.asarray(Image.open(_packshot(tmp_path / "p.png")).convert("RGB"))
    box = product_box(product_mask(rgb))
    assert box is not None
    assert abs(box.x0 - BODY[0]) <= 2 and abs(box.x1 - BODY[2]) <= 2
    assert abs(box.y0 - CAP[1]) <= 2 and abs(box.y1 - BODY[3]) <= 2


def test_cap_crop_frames_the_cap_at_the_frame_aspect(tmp_path: Path) -> None:
    rgb = np.asarray(Image.open(_packshot(tmp_path / "p.png")).convert("RGB"))
    b = crop_box(product_mask(rgb), "cap", 9 / 16)
    assert abs(b.w / b.h - 9 / 16) < 0.01
    # the whole cap is inside, near the top, and it fills more than half the crop's width
    assert b.x0 <= CAP[0] and b.x1 >= CAP[2] and b.y0 <= CAP[1] < b.y0 + b.h // 3
    assert (CAP[2] - CAP[0]) / b.w > 0.5
    assert b.h < H  # a close-up, not the whole packshot


def test_edge_crop_holds_the_right_shoulder(tmp_path: Path) -> None:
    rgb = np.asarray(Image.open(_packshot(tmp_path / "p.png")).convert("RGB"))
    b = crop_box(product_mask(rgb), "edge", 9 / 16)
    assert b.x0 < BODY[2] < b.x1  # the body's right edge runs through the frame
    assert b.w < (BODY[2] - BODY[0])


def test_crop_is_deterministic_and_sized_to_the_frame(tmp_path: Path) -> None:
    src = _packshot(tmp_path / "p.png")
    a = macro_crop(src, tmp_path / "a.png", region="cap", width=720, height=1280)
    b = macro_crop(src, tmp_path / "b.png", region="cap", width=720, height=1280)
    assert a.read_bytes() == b.read_bytes()
    assert Image.open(a).size == (720, 1280)


def test_no_product_found_still_gives_a_valid_crop() -> None:
    mask = np.zeros((H, W), dtype=bool)
    b = crop_box(mask, "cap", 9 / 16)
    assert 0 <= b.x0 < b.x1 <= W and 0 <= b.y0 < b.y1 <= H


def test_crop_mode_needs_a_region() -> None:
    base = {
        "label": "m", "framing": "macro", "needs_product": True, "shows_person": False,
        "shot_type": "extreme_close_up", "camera": "push_in", "speed": "slow_motion",
        "lighting": "l", "motion": "m", "keyframe_hint": "k", "fallback": "x",
    }  # fmt: skip
    assert ShotVocab.model_validate({**base, "identity_mode": "crop", "crop": "cap"}).crop == "cap"
    with pytest.raises(ValidationError):
        ShotVocab.model_validate({**base, "identity_mode": "crop"})
    with pytest.raises(ValidationError):
        ShotVocab.model_validate({**base, "identity_mode": "direct", "crop": "cap"})
