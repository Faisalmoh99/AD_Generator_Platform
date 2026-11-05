"""Record the silent demo footage, one file per segment of docs/DEMO_SCRIPT.md.

Playwright video of a 1670x940 viewport, scaled to 1920x1080 (the page reads at ~115 %), a
visible cursor overlay with a click ripple, smooth mouse moves and a pause on everything the
narration points at. Three browser sessions; each segment's start and end are marked in time
and cut out of the session video with ffmpeg, then padded (last frame held) or trimmed to its
exact duration.

    # A: real runs (default data dir)          B: live mock runs (fresh data dir), see DEMO_SCRIPT
    uv run adsmith serve --port 8793 &
    ADSMITH_DATA_DIR=... ADSMITH_SCRIPTED_SCENARIO=montage_en ADSMITH_MOCK_LATENCY_S=2.2 \\
        ADSMITH_SCRIPTED_LATENCY_S=2.5 uv run adsmith serve --port 8792 &
    uv run --with playwright python scripts/record_demo.py
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from demo_media import concat, normalise, profile
from playwright.sync_api import Browser, Locator, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs" / "demo"
SEG = DEMO / "segments"
V2 = "run_01M6MVHD6CE5V22VM38GXNRN09"
REQUEST = "سوّ لي إعلان 12 ثانية لعطر رجالي فاخر للشباب، فخم وجريء"
VIEW = {"width": 1670, "height": 940}
VIDEO = {"width": 1920, "height": 1080}
TARGETS = {
    "01_hook": 30.0, "02_brain": 60.0, "03_chaos_run": 75.0, "04_edit": 45.0,
    "05_real_runs": 60.0, "06_resume_limits": 30.0,
}  # fmt: skip

CURSOR_JS = (DEMO / "cursor.js").read_text(encoding="utf-8")


@dataclass
class Session:
    """One browser page recording a video; segments are time ranges of it."""

    page: Page
    t0: float
    x: float = VIEW["width"] / 2
    y: float = VIEW["height"] / 2
    marks: dict[str, list[float]] = field(default_factory=dict)

    def now(self) -> float:
        return time.monotonic() - self.t0

    def start(self, name: str) -> None:
        self.marks[name] = [self.now()]

    def end(self, name: str) -> None:
        self.marks[name].append(self.now())

    def wait(self, s: float) -> None:
        self.page.wait_for_timeout(int(s * 1000))

    def goto(self, url: str, settle: float = 1.5) -> None:
        self.page.goto(url)
        if "#" in url:
            self.page.reload()  # a hash-only change does not reboot the app (it reads it at boot)
        self.wait(settle)
        self.page.mouse.move(self.x, self.y)  # the overlay reappears where the mouse was

    def move(self, x: float, y: float, dur: float = 0.7) -> None:
        n = max(8, int(dur * 50))
        x0, y0 = self.x, self.y
        for i in range(1, n + 1):
            t = i / n
            e = 0.5 - 0.5 * math.cos(math.pi * t)  # ease in-out
            self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            self.page.wait_for_timeout(int(dur * 1000 / n))
        self.x, self.y = x, y

    def box(self, target: str | Locator) -> dict[str, float]:
        """The target's box; live views redraw their SVG, so a detached element is looked up
        again (a selector re-resolves on each try)."""
        last: Exception | None = None
        for _ in range(6):
            loc = self.page.locator(target).first if isinstance(target, str) else target
            try:
                loc.scroll_into_view_if_needed(timeout=3000)
                b = loc.bounding_box(timeout=3000)
            except PlaywrightError as exc:
                last = exc
                self.page.wait_for_timeout(120)
                continue
            if b is not None:
                return b
        raise RuntimeError(f"no box for {target}: {last}")

    def point(self, target: str | Locator, pause: float = 1.5, fx: float = 0.5, fy: float = 0.5,
              dur: float = 0.7) -> None:  # fmt: skip
        b = self.box(target)
        self.move(b["x"] + b["width"] * fx, b["y"] + b["height"] * fy, dur)
        self.wait(pause)

    def click(self, target: str | Locator, pause: float = 0.8) -> None:
        self.point(target, pause=0.35)
        self.page.mouse.down()
        self.wait(0.08)
        self.page.mouse.up()
        self.wait(pause)

    def scroll_to(self, selector: str, block: str = "start", pause: float = 1.0) -> None:
        self.page.evaluate(
            "([s, b]) => document.querySelector(s).scrollIntoView({behavior: 'smooth', block: b})",
            [selector, block],
        )
        self.wait(pause)


def new_session(browser: Browser, tmp: Path) -> tuple[Any, Session]:
    ctx = browser.new_context(
        # recorded at the viewport's size, then scaled to 1920x1080 when cut: the page reads at
        # ~115 % (Playwright pads, never scales, a larger video size)
        viewport=VIEW,
        record_video_dir=str(tmp),
        record_video_size=VIEW,
        device_scale_factor=1,
    )
    ctx.add_init_script(CURSOR_JS)
    t0 = time.monotonic()
    page = ctx.new_page()
    return ctx, Session(page=page, t0=t0)


def api(base: str, path: str) -> Any:
    import urllib.request  # noqa: PLC0415

    with urllib.request.urlopen(f"{base}{path}") as r:
        return json.load(r)


def wait_view(base: str, run_id: str, pred: Any, timeout: float = 240) -> dict[str, Any]:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        v: dict[str, Any] = api(base, f"/api/runs/{run_id}")
        if pred(v):
            return v
        time.sleep(0.2)
    raise TimeoutError(run_id)


# ---------------------------------------------------------------------------- session A: real runs
def session_real(s: Session, base: str) -> None:  # noqa: PLR0915 - a linear script
    # hook, second half: the landing page (17.5 s after the v2 clip)
    s.goto(f"{base}/", settle=3.0)
    s.start("01_hook")
    s.move(820, 300, 0.9)
    s.point("#request", 2.0, fx=0.8)
    s.point("#go", 1.5)
    cards = s.page.locator(".teaser-card")
    s.point(cards.nth(0), 2.2)
    s.point(cards.nth(1), 2.2)
    s.point(cards.nth(2), 1.4, fx=0.27, fy=0.45)
    s.point(cards.nth(2), 2.2, fx=0.75, fy=0.45)
    s.end("01_hook")

    # 2 · the brain: the real v2 run
    s.goto(f"{base}/#run={V2}", settle=2.5)
    s.page.evaluate("document.querySelector('#decisions').scrollTop = 0")
    s.start("02_brain")
    s.point("#graph", 2.5, fy=0.3)
    nodes = s.page.locator("#graph .node")
    for i, pause in ((0, 3.2), (2, 5.0), (6, 3.3), (7, 3.0), (8, 4.6), (9, 2.8)):
        s.point(nodes.nth(i), pause)
    gates = s.page.locator("#graph rect.gate")
    s.point(gates.nth(0), 3.3)
    s.point("#graph path.loop", 2.4)
    items = s.page.locator("#decisions li")
    for text, pause in (("montage format", 2.6), ("picked concept", 3.2), ("swapped", 3.2),
                        ("Beat cut", 3.0)):  # fmt: skip
        item = items.filter(has_text=text).first
        if item.count():
            item.evaluate("e => e.scrollIntoView({block: 'nearest', behavior: 'smooth'})")
            s.wait(0.4)
            s.point(item, pause, fx=0.6)
    s.point("#burn", 1.8)
    s.end("02_brain")

    # 5 · real runs: the curtain before/after in the gallery, then the lessons page
    s.click(".tab[data-view=gallery]", pause=2.0)
    card = s.page.locator(".g-card", has_text="curtain veil")
    card.evaluate("e => e.scrollIntoView({block: 'start'})")
    s.page.evaluate("window.scrollBy(0, -56)")  # below the sticky top bar
    s.wait(0.6)
    s.page.evaluate(
        "() => [...document.querySelectorAll('.g-card')]"
        ".find(c => c.textContent.includes('curtain'))"
        ".querySelectorAll('video').forEach(v => { v.currentTime = 0; v.play(); })"
    )
    s.start("05_real_runs")
    vids = card.locator(".g-video")
    s.point(vids.nth(0), 4.0, fy=0.45)
    s.point(card.locator(".vlabel").nth(0), 2.3)
    s.point(vids.nth(1), 4.0, fy=0.45)
    s.point(card.locator(".vlabel").nth(1), 2.3)
    s.point(card.locator("p").first, 2.4, fx=0.3)
    s.goto((DEMO / "lessons.html").as_uri(), settle=1.2)
    s.point("#calibration .step.bad >> nth=0", 3.0, fx=0.3)
    s.point("#calibration .step.bad >> nth=1", 3.0, fx=0.3)
    s.point("#calibration .step.good", 3.0, fx=0.3)
    s.point("#calibration .big", 2.5)
    s.point("#kurta img", 3.5, fy=0.35)
    s.point("#kurta .lint", 3.0, fx=0.3)
    s.point("#kurta .fix", 3.0, fx=0.3)
    s.point("#gate .big >> nth=0", 2.5)
    s.point("#gate .big >> nth=1", 3.0)
    s.point("#gate img", 1.8, fy=0.3)
    s.end("05_real_runs")


# ---------------------------------------------------------------------------- session B: live run
def session_live(s: Session, base: str, real: str) -> None:
    s.goto(f"{base}/", settle=3.0)
    s.page.fill("#request", REQUEST)
    s.start("03_chaos_run")
    s.point("#request", 1.5, fx=0.85)
    s.click("label.check:has(#chaos)", pause=1.0)
    s.click("#go", pause=0.5)
    s.page.wait_for_function("location.hash.includes('run=')")
    rid = str(s.page.evaluate("location.hash.split('run=')[1]"))
    t_run = time.monotonic()
    # follow the lit stage in the graph until the scene lanes fill in
    while True:
        v = api(base, f"/api/runs/{rid}")
        if v["stage"] in {"done", "done_partial", "errored"}:
            break
        if s.page.locator("#graph .node.active").count() and v["stage"] not in {"generate", "qa"}:
            s.point("#graph .node.active", 1.2, fy=0.75, dur=0.6)
        elif s.page.locator("#lanes .row").count():
            k = int((time.monotonic() - t_run) // 3) % s.page.locator("#lanes .row").count()
            s.point(f"#lanes .row >> nth={k}", 2.2, fx=0.12, dur=0.8)
        else:
            s.wait(0.5)
    # what is left of the 75 s goes to the loops' reasons, then ~6 s for the real run's fallback
    left = TARGETS["03_chaos_run"] - (s.now() - s.marks["03_chaos_run"][0]) - 7.0
    hovers = [sel for sel in ("#lanes .step.tag:has-text('retry')",
              "#lanes .step.tag:has-text('policy')", "#lanes .step.qa.fail")
              if s.page.locator(sel).count()]  # fmt: skip
    each = max(1.6, (left - 0.8 * len(hovers)) / max(1, len(hovers)))
    for sel in hovers:
        s.point(sel, each)
    s.goto(f"{real}/#run={V2}", settle=1.4)
    s.point("#lanes .step.tag:has-text('bottle_on_rock')", 4.0)
    s.end("03_chaos_run")

    # 4 · the edit, on the finished chaos run
    s.goto(f"{base}/#run={rid}", settle=2.5)
    s.page.evaluate("document.querySelector('#board-wrap').scrollIntoView({block: 'start'})")
    s.page.evaluate("window.scrollBy(0, -12)")
    s.wait(0.8)
    s.start("04_edit")
    s.click("#edit", pause=0.4)
    s.page.keyboard.type("خل المشهد الثاني أبطأ", delay=110)
    s.wait(0.8)
    s.click("#edit-go", pause=1.5)
    s.page.wait_for_selector("#blast:not(.hidden)")
    s.point("#blast", 4.0, fx=0.35)
    s.point("#board .scene >> nth=1", 4.0, fy=0.6)
    s.point("#board .scene >> nth=4", 3.0, fy=0.6)
    wait_view(base, rid, lambda v: v["stage"] in {"done", "done_partial"} and not v["busy"])
    s.wait(1.5)
    s.point("#board .scene >> nth=1", 3.0, fy=0.85)
    s.scroll_to("#lanes-wrap", block="center", pause=1.2)
    s.point("#lanes .row >> nth=1", 4.0, fx=0.1)
    s.point("#lanes .row >> nth=1", 3.0, fx=0.45)
    s.end("04_edit")
    print("chaos run", rid)


# ---------------------------------------------------------------------------- session C: terminal
def session_terminal(s: Session) -> None:
    data = json.loads((DEMO / "make_demo.json").read_text())
    home = str(ROOT) + "/"
    data["frames"] = [
        [t, c.replace(home, "~/adsmith/")] for t, c in data["frames"]
    ]  # no local paths
    s.goto((DEMO / "terminal.html").as_uri(), settle=1.5)
    s.move(1500, 880, 0.2)
    gaps, prev = 0.0, 0.0
    for ts, _ in data["frames"]:
        gaps += min(ts - prev, 1.4)
        prev = ts
    speed = gaps / 19.0  # the replay lasts ~19 s
    s.start("06_resume_limits")
    s.page.evaluate("([d, sp]) => { window.play(d, sp, 1.4); }", [data, speed])  # do not await
    s.wait(19.8)
    s.goto((DEMO / "limits.html").as_uri(), settle=0.6)
    for i in range(1, 5):
        s.point(f"#l{i} h2", 1.35, fx=0.3)
    s.end("06_resume_limits")


# ---------------------------------------------------------------------------- cutting
def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def duration(path: Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(probe.stdout.strip())


def cut(src: Path, start: float, end: float, target: float, out: Path) -> None:
    """[start, end] of the session video, scaled to 1920x1080, trimmed to ``target`` or padded to
    it by holding the last frame. Input-side -t, so the padding is not cut off again."""
    dur = end - start
    pad = max(0.0, target - dur) + 0.5
    vf = (f"scale={VIDEO['width']}:{VIDEO['height']}:flags=lanczos,fps=30,format=yuv420p,"
          f"tpad=stop_mode=clone:stop_duration={pad:.3f}")  # fmt: skip
    ffmpeg("-ss", f"{start:.3f}", "-t", f"{min(dur, target):.3f}", "-i", str(src), "-vf", vf,
           "-t", f"{target:.3f}", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-an",
           str(out))  # fmt: skip
    print(f"{out.name}: {dur:.1f}s recorded → {duration(out):.2f}s (target {target:.0f}s)")


def fit(path: Path, target: float) -> None:
    """Hold the last frame (or trim) so a finished segment lasts exactly ``target``."""
    have = duration(path)
    if abs(have - target) < 0.05:
        return
    tmp = path.with_suffix(".fit.mp4")
    hold = max(0.0, target - have) + 0.5
    ffmpeg("-i", str(path), "-vf", f"tpad=stop_mode=clone:stop_duration={hold:.3f}",
           "-t", f"{target:.3f}", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-an",
           str(tmp))  # fmt: skip
    tmp.replace(path)
    print(f"{path.name}: {have:.2f}s → {duration(path):.2f}s")


def hook(landing: Path, out: Path) -> None:
    """montage_v2.mp4 centred on black (12.5 s), then the recorded landing page, 30 s in all.
    Both pieces are normalised and joined with a re-encode (a stream-copy join froze QuickTime
    at 12.5 s: one avcC for two differently encoded clips). The segment stays silent: the music
    bed is added by merge_voiceover.py."""
    v2 = ROOT / "docs" / "media" / "montage_v2.mp4"
    pad = out.with_suffix(".pad.mp4")
    vf = "scale=-2:1080,pad=1920:1080:(ow-iw)/2:0:color=0x0e0f10"
    ffmpeg("-i", str(v2), "-vf", vf, "-an", "-c:v", "libx264", "-crf", "16", str(pad))
    a, b = out.with_suffix(".a.mp4"), out.with_suffix(".b.mp4")
    normalise(pad, a, keep_audio=False)
    normalise(landing, b, seconds=TARGETS["01_hook"] - duration(a), keep_audio=False)
    concat([a, b], out)
    for p in (pad, a, b):
        p.unlink()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", default="http://localhost:8793")
    ap.add_argument("--live", default="http://localhost:8792")
    ap.add_argument("--only", nargs="*", default=["real", "live", "terminal"])
    ap.add_argument("--fit-only", action="store_true", help="only fit existing segments and stitch")
    args = ap.parse_args()
    SEG.mkdir(parents=True, exist_ok=True)
    if args.fit_only:
        args.only = []
    tmp = DEMO / ".raw"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    sessions: list[tuple[Session, Path]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for name in args.only:
            ctx, s = new_session(browser, tmp)
            if name == "real":
                session_real(s, args.real)
            elif name == "live":
                session_live(s, args.live, args.real)
            else:
                session_terminal(s)
            video = s.page.video
            ctx.close()
            assert video is not None
            sessions.append((s, Path(video.path())))
        browser.close()
    for s, src in sessions:
        for name, (start, end) in s.marks.items():
            if name == "01_hook":
                landing = SEG / "01_hook.landing.mp4"
                cut(src, start, end, TARGETS[name] - 12.5, landing)
                hook(landing, SEG / "01_hook.mp4")
                landing.unlink()
            else:
                cut(src, start, end, TARGETS[name], SEG / f"{name}.mp4")
    shutil.rmtree(tmp, ignore_errors=True)
    for name, target in TARGETS.items():
        seg = SEG / f"{name}.mp4"
        if seg.exists():
            fit(seg, target)
            tmp_n = seg.with_suffix(".norm.mp4")
            normalise(seg, tmp_n, seconds=target, keep_audio=False)  # + a silent stereo track
            tmp_n.replace(seg)
    files = [SEG / f"{n}.mp4" for n in TARGETS]
    if all(f.exists() for f in files):
        draft = DEMO / "draft_silent.mp4"
        concat(files, draft)  # one re-encode, never stream copy
        print(draft, f"{duration(draft):.2f}s")
        print(profile(draft))


if __name__ == "__main__":
    main()
