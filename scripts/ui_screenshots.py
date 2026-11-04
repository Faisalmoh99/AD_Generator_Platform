"""Drive the web UI with Playwright (installed Chrome) and capture the demo screenshots.

    uv run adsmith serve --port 8765 &      # zero-key: scripted LLM + mock media
    uv run --with playwright python scripts/ui_screenshots.py --base http://localhost:8765 \
        --out docs/screenshots
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

REQUEST = "اعطني إعلان ١٥ ثانية لعطر رجالي فاخر يستهدف الشباب بأسلوب سينمائي"


def view(base: str, run_id: str) -> dict[str, object]:
    with urllib.request.urlopen(f"{base}/api/runs/{run_id}") as r:
        data: dict[str, object] = json.load(r)
    return data


def wait(base: str, run_id: str, pred: object, timeout: float = 180) -> dict[str, object]:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        v = view(base, run_id)
        if pred(v):  # type: ignore[operator]
            return v
        time.sleep(0.3)
    raise TimeoutError("UI state not reached")


def shot(page: Page, out: Path, name: str, selector: str | None = None) -> None:
    page.wait_for_timeout(700)
    path = out / f"{name}.png"
    if selector:
        page.locator(selector).screenshot(path=str(path))
    else:
        page.screenshot(path=str(path))
    print(path)


def main() -> None:  # noqa: PLR0915 - linear UI script
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8765")
    ap.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.base
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1.5)
        page.goto(base)
        page.fill("#request", REQUEST)
        page.evaluate("document.querySelector('#auto').checked = false")
        page.evaluate("document.querySelector('#chaos').checked = true")
        page.click("#go")
        page.wait_for_function("location.hash.includes('run=')")
        run_id = page.evaluate("location.hash.split('run=')[1]")
        wait(base, run_id, lambda v: v["stage"] == "awaiting_concept_choice")
        page.locator("#concepts-wrap").scroll_into_view_if_needed()
        shot(page, args.out, "1_concepts")

        page.click("[data-pick='3']")
        wait(base, run_id, lambda v: v["stage"] == "awaiting_storyboard_approval")
        page.wait_for_timeout(1200)
        page.click("#approve")

        def mid_run(v: dict[str, object]) -> bool:
            scenes = v["scenes"]  # type: ignore[index]
            states = {s["status"] for s in scenes}  # type: ignore[attr-defined,index]
            busy = bool(states & {"generating", "qa_review"})
            return bool(busy and v["stage"] in {"generate", "qa"})

        wait(base, run_id, mid_run)
        page.wait_for_function(
            "document.querySelectorAll("
            "'#trace li.t-recovery, #trace li.t-retry_scheduled').length > 0",
            timeout=60_000,
        )
        page.locator("#board-wrap").scroll_into_view_if_needed()
        shot(page, args.out, "2_storyboard_midrun")

        wait(base, run_id, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
        page.wait_for_timeout(1500)
        page.locator("#final-wrap").scroll_into_view_if_needed()
        shot(page, args.out, "3_final_player")

        page.fill("#edit", "make scene 3 at night")
        page.click("#edit-go")
        wait(base, run_id, lambda v: v["blast"] is not None)
        page.wait_for_timeout(1200)
        page.locator("#board-wrap").scroll_into_view_if_needed()
        shot(page, args.out, "4_edit_blast_radius")
        v = wait(base, run_id, lambda v: not v["busy"] and v["stage"] != "prompt")
        if v["stage"] == "awaiting_storyboard_approval":  # interactive: re-approve new keyframes
            page.click("#approve")
        wait(base, run_id, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
        page.wait_for_timeout(1000)
        page.locator("#board-wrap").scroll_into_view_if_needed()
        shot(page, args.out, "4b_edit_done")

        page.click(".tab[data-view='gallery']")
        page.wait_for_selector(".g-card")
        page.wait_for_timeout(2500)
        shot(page, args.out, "5_real_runs_gallery")
        browser.close()


if __name__ == "__main__":
    main()
