"""Review the redesigned studio with Playwright (installed Chrome): landing, a live montage run
(pipeline graph, swimlanes with loops, budget burn, decision log), the finished run, a lane
tooltip, an interactive gate, the gallery, and a laptop width. Prints every screenshot path and
any console error.

    ADSMITH_SCRIPTED_SCENARIO=montage_en ADSMITH_MOCK_LATENCY_S=0.9 \
        uv run adsmith serve --port 8790 &
    uv run --with playwright python scripts/ui_studio_review.py --base http://localhost:8790 \
        --out docs/screenshots/ui
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

REQUEST = "سوّ لي إعلان 12 ثانية لعطر رجالي فاخر للشباب، فخم وجريء"


def view(base: str, run_id: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base}/api/runs/{run_id}") as r:
        data: dict[str, Any] = json.load(r)
    return data


def wait(base: str, run_id: str, pred: Any, timeout: float = 240) -> dict[str, Any]:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        v = view(base, run_id)
        if pred(v):
            return v
        time.sleep(0.25)
    raise TimeoutError("UI state not reached")


def shot(page: Page, out: Path, name: str, selector: str | None = None, full: bool = False) -> None:
    page.wait_for_timeout(600)
    path = out / f"{name}.png"
    if selector:
        page.locator(selector).first.screenshot(path=str(path))
    else:
        page.screenshot(path=str(path), full_page=full)
    print(path)


def run_id_of(page: Page) -> str:
    page.wait_for_function("location.hash.includes('run=')")
    return str(page.evaluate("location.hash.split('run=')[1]"))


def main() -> None:  # noqa: PLR0915 - linear UI script
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8790")
    ap.add_argument("--out", type=Path, default=Path("docs/screenshots/ui"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    base, out = args.base, args.out
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1.5)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.wait_for_timeout(1500)
        shot(page, out, "01_landing")

        # live montage run, chaos on (retries, rewrites and QA loops in the lanes)
        page.fill("#request", REQUEST)
        page.evaluate("document.querySelector('#chaos').checked = true")
        page.click("#go")
        rid = run_id_of(page)
        wait(base, rid, lambda v: v["stage"] in {"keyframe", "generate", "qa"} and len(v["scenes"]))
        wait(base, rid, lambda v: v["stage"] in {"generate", "qa"})
        page.wait_for_timeout(2500)
        shot(page, out, "02_live_run")
        shot(page, out, "03_live_pipeline", ".block")
        wait(base, rid, lambda v: v["stage"] in {"done", "done_partial", "errored"})
        page.wait_for_timeout(2000)
        shot(page, out, "04_done_full", full=True)
        shot(page, out, "05_done_pipeline", ".block")
        shot(page, out, "06_panel", ".panel")
        # tooltip on a lane step with a reason
        steps = page.locator("#lanes .step.fail, #lanes .step.tag")
        target = steps.first if steps.count() else page.locator("#lanes .step").first
        target.hover()
        page.wait_for_timeout(400)
        shot(page, out, "07_lane_tooltip")

        # an edit: blast radius on the board
        page.fill("#edit", "خل المشهد الثاني أبطأ")
        page.click("#edit-go")
        page.wait_for_timeout(1500)
        wait(base, rid, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
        page.wait_for_timeout(1500)
        shot(page, out, "08_after_edit", "#board-wrap")

        # interactive run: the human gate lights up
        page.goto(base)
        page.wait_for_timeout(800)
        page.fill("#request", REQUEST)
        page.evaluate("document.querySelector('#auto').checked = false")
        page.click("#go")
        rid2 = run_id_of(page)
        wait(base, rid2, lambda v: v["stage"] == "awaiting_concept_choice")
        page.wait_for_timeout(1500)
        shot(page, out, "09_concept_gate")

        page.click(".tab[data-view=gallery]")
        page.wait_for_timeout(2500)
        shot(page, out, "10_gallery")

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{base}/#run={rid}")
        page.wait_for_timeout(2500)
        shot(page, out, "11_laptop")
        browser.close()
    print("console errors:", errors or "none")


if __name__ == "__main__":
    main()
