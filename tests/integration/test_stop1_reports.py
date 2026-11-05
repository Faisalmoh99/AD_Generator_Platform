"""STOP 1 tooling: the contact sheet and the decisions report render from a montage run."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from PIL import Image

from adsmith.config import PROJECT_ROOT
from adsmith.domain.enums import Stage
from tests.media_helpers import fast_settings, make_harness

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."


async def test_contact_sheet_and_decisions_report(tmp_path: Path) -> None:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        sheet_mod = importlib.import_module("contact_sheet")
        report_mod = importlib.import_module("decisions_report")
    finally:
        sys.path.pop(0)
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id, stop_before=Stage.GENERATE)
    assert state.stage is Stage.GENERATE

    path = sheet_mod.build(h.rt, run.id)
    with Image.open(path) as img:
        assert img.width > 1000 and img.height > 1000

    text = report_mod.decisions_text(h.rt, state)
    assert "1. FORMAT: montage" in text and "physical product" in text
    assert "* 1. Obsidian" in text
    assert "3. PRODUCT REFERENCE" in text and "APPROVED" in text
    assert "S1 hook" in text and "cap_macro · crop" in text
    assert "S8 reveal" in text and "hand_cuff · composite" in text
    assert "spray_from_nozzle · direct" in text and "water_drop_leaf · none" in text
