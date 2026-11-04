"""First-time-evaluator walkthrough of the web UI (Playwright + installed Chrome): screenshots at
every step, console errors, Production Pack download, gallery playback, laptop width.

    uv run --with playwright python scripts/ui_review.py --base http://localhost:8766 \
        --out docs/screenshots/review
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

REQUEST = "اعطني إعلان ١٥ ثانية لعطر رجالي فاخر يستهدف الشباب بأسلوب سينمائي"


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
        time.sleep(0.3)
    raise TimeoutError(view(base, run_id)["stage"])


def finished(v: dict[str, Any]) -> bool:
    return v["stage"] in {"done", "done_partial"} and not v["busy"]


class Shooter:
    def __init__(self, page: Page, out: Path) -> None:
        self.page, self.out, self.n = page, out, 0

    def __call__(self, name: str, full: bool = False) -> None:
        self.page.wait_for_timeout(600)
        path = self.out / f"{self.n:02d}_{name}.png"
        self.page.screenshot(path=str(path), full_page=full)
        self.n += 1
        print(path)


def edit(page: Page, base: str, run_id: str, text: str, shot: Shooter, name: str) -> None:
    page.fill("#edit", text)
    page.click("#edit-go")
    wait(base, run_id, lambda v: v["blast"] is not None and text[:8] in v["blast"]["edit"])
    page.locator("#board-wrap").scroll_into_view_if_needed()
    shot(f"{name}_blast")
    v = wait(base, run_id, lambda v: not v["busy"] and v["stage"] != "prompt")
    if v["stage"] == "awaiting_storyboard_approval":
        page.locator("#approve").scroll_into_view_if_needed()
        shot(f"{name}_reapprove_gate")
        page.click("#approve")
    wait(base, run_id, finished)


def main() -> None:  # noqa: PLR0915 - linear walkthrough
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8766")
    ap.add_argument("--out", type=Path, default=Path("docs/screenshots/review"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.base
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=1.25,
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        shot = Shooter(page, args.out)

        page.goto(base)
        shot("landing")
        page.fill("#request", REQUEST)
        page.evaluate("document.querySelector('#auto').checked = false")
        page.evaluate("document.querySelector('#chaos').checked = true")
        page.click("#go")
        page.wait_for_function("location.hash.includes('run=')")
        run_id = page.evaluate("location.hash.split('run=')[1]")
        page.wait_for_timeout(1500)
        shot("just_submitted")
        wait(base, run_id, lambda v: v["stage"] == "awaiting_concept_choice")
        page.locator("#concepts-wrap").scroll_into_view_if_needed()
        shot("concepts")
        page.click("[data-pick='3']")
        wait(base, run_id, lambda v: v["stage"] == "awaiting_storyboard_approval" and not v["busy"])
        page.locator("#board-wrap").scroll_into_view_if_needed()
        shot("keyframes_gate")
        page.click("#approve")
        wait(base, run_id, lambda v: any(s["recoveries"] for s in v["scenes"]) and v["busy"])
        page.locator("#board-wrap").scroll_into_view_if_needed()
        shot("midrun_chaos")
        wait(base, run_id, finished)
        page.wait_for_timeout(1200)
        page.locator("#final-wrap").scroll_into_view_if_needed()
        shot("final")
        page.evaluate("document.querySelector('#final-video').play()")
        page.wait_for_timeout(16500)  # land on the end card
        shot("final_endcard")
        shot("full_page_done", full=True)

        edit(page, base, run_id, "خل المشهد الثاني أبطأ", shot, "edit1_slower")
        edit(page, base, run_id, "make scene 3 at night", shot, "edit2_night")

        for f in ("recovery", "qa", "cost", "edit"):
            page.click(f".filter[data-f='{f}']")
            page.wait_for_timeout(300)
            shot(f"trace_filter_{f}")
        page.click(".filter[data-f='all']")

        with page.expect_download() as dl:
            page.click("#pack")
        pack = args.out / "pack.zip"
        dl.value.save_as(str(pack))
        names = zipfile.ZipFile(pack).namelist()
        print("pack:", names)

        page.click(".tab[data-view='gallery']")
        page.wait_for_selector(".g-card")
        page.wait_for_timeout(1500)
        shot("gallery_top")
        playing = page.evaluate("""async () => {
            const out = [];
            for (const v of document.querySelectorAll('.g-video video')) {
                try { await v.play(); await new Promise(r => setTimeout(r, 700));
                      out.push({src: v.src.split('/').pop(), t: v.currentTime,
                                ready: v.readyState});
                      v.pause(); }
                catch (e) { out.push({src: v.src, error: String(e)}); }
            }
            return out; }""")
        print("gallery playback:", playing)
        page.locator(".g-card").nth(2).scroll_into_view_if_needed()
        shot("gallery_curtain")

        page.set_viewport_size({"width": 1280, "height": 800})
        page.click(".tab[data-view='studio']")
        page.evaluate("window.scrollTo(0, 0)")
        shot("laptop_top")
        page.locator("#concepts-wrap").scroll_into_view_if_needed()
        shot("laptop_concepts")
        page.locator("#board-wrap").scroll_into_view_if_needed()
        shot("laptop_board")
        page.locator("#final-wrap").scroll_into_view_if_needed()
        shot("laptop_final")
        page.click(".tab[data-view='gallery']")
        page.wait_for_timeout(800)
        shot("laptop_gallery")
        browser.close()
    print("console errors:", errors or "none")


if __name__ == "__main__":
    main()
