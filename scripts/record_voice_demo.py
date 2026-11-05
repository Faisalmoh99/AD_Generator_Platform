"""Record the demo footage timed to the narration (video follows voice).

Each segment's screen actions are scheduled at the moment the narrator talks about them: cue time
= the word's time in the final voice track (faster-whisper word timestamps of
docs/demo/voice/NN_name.wav) + the segment's voice lead (3.0 s in the hook, 0.25 s elsewhere).
The cursor starts moving 0.7 s early so it arrives on the word, then holds still until the next
cue. Segments last exactly lead + voice + 0.8 s.

    uv run adsmith serve --port 8793 &                       # real runs (v2, gallery)
    ADSMITH_DATA_DIR=.../data ADSMITH_SCRIPTED_SCENARIO=montage_en ADSMITH_MOCK_LATENCY_S=0.45 \\
        ADSMITH_SCRIPTED_LATENCY_S=0.3 uv run adsmith serve --port 8792 &   # live chaos run
    uv run --with playwright python scripts/record_voice_demo.py --words <dir with NN_name.json>
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from demo_media import concat, duration, ffmpeg, normalise, profile
from playwright.sync_api import Locator, sync_playwright
from record_demo import DEMO, REQUEST, SEG, V2, Session, new_session

VOICE = DEMO / "voice"
LEAD = {"01_hook": 3.0}
TAIL = 0.8
ARRIVE = 0.7
V2_CLIP = 12.54  # montage_v2.mp4 opens the hook


class Words:
    def __init__(self, words_dir: Path) -> None:
        self.dir = words_dir
        self.cache: dict[str, list[tuple[float, str]]] = {}

    def __call__(self, seg: str, needle: str, after: float, default: float) -> float:
        """Voice time of the first word containing ``needle`` at or after ``after`` (s)."""
        if seg not in self.cache:
            data = json.loads((self.dir / f"{seg}.json").read_text())
            self.cache[seg] = [
                (w["start"], w["word"]) for s in data["segments"] for w in s["words"]
            ]
        for t, w in self.cache[seg]:
            if t >= after - 0.05 and re.search(needle, w):
                return float(t)
        print(f"  ! {seg}: '{needle}' not found after {after}s; using {default}s")
        return default


class Timeline:
    """Runs a segment's cues against the clock: ``at(t)`` returns when the cursor should leave
    to arrive at t; actions given to ``do`` run then."""

    def __init__(self, s: Session, name: str, offset: float = 0.0) -> None:
        self.s, self.name = s, name
        self.lead = LEAD.get(name, 0.25)
        self.offset = offset  # video time at which this recording starts (hook: after the ad)
        self.t0 = 0.0

    def start(self) -> None:
        self.s.start(self.name)
        self.t0 = time.monotonic()

    def video_t(self, voice_t: float) -> float:
        return voice_t + self.lead - self.offset

    def wait_until(self, video_t: float) -> None:
        left = video_t - (time.monotonic() - self.t0)
        if left > 0:
            self.s.wait(left)
        elif left < -0.6:
            print(f"  ~ {self.name}: {-left:.1f}s behind at {video_t + self.offset:.1f}s")

    def point(self, voice_t: float, target: str | Locator, fallback: str | None = None,
              **kw: Any) -> None:  # fmt: skip
        """Arrive on ``target`` at ``voice_t``. A live view may not have drawn it yet: wait at
        most 2.5 s for it, then hold on ``fallback`` (or stay put) instead of stalling."""
        self.wait_until(self.video_t(voice_t) - ARRIVE)
        loc = self.s.page.locator(target).first if isinstance(target, str) else target
        deadline = time.monotonic() + 2.5
        while loc.count() == 0 and time.monotonic() < deadline:
            self.s.wait(0.15)
        if loc.count() == 0:
            print(f"  ! {self.name}: target missing at {voice_t:.1f}s; holding")
            if fallback:
                self.s.point(fallback, pause=0.0, dur=ARRIVE)
            return
        self.s.point(loc, pause=0.0, dur=ARRIVE, **kw)

    def do(self, voice_t: float, fn: Callable[[], None]) -> None:
        self.wait_until(self.video_t(voice_t))
        fn()

    def end(self, total_video: float) -> None:
        self.wait_until(total_video - self.offset)
        self.s.end(self.name)


def voice_len(name: str) -> float:
    return duration(VOICE / f"{name}.wav")


def seg_len(name: str) -> float:
    return round(LEAD.get(name, 0.25) + voice_len(name) + TAIL, 2)


def scroll_block(s: Session, selector: str, offset: int = 56) -> None:
    s.page.evaluate(
        "([sel, off]) => { const e = document.querySelector(sel);"
        " window.scrollTo({top: e.getBoundingClientRect().top + window.scrollY - off,"
        " behavior: 'smooth'}); }",
        [selector, offset],
    )
    s.wait(0.9)


# ---------------------------------------------------------------------------------- segments
def rec_hook(s: Session, real: str, w: Words) -> None:
    name = "01_hook"
    tl = Timeline(s, name, offset=V2_CLIP)
    s.goto(f"{real}/", settle=3.0)
    s.page.evaluate("() => document.querySelectorAll('#teaser video').forEach(v => v.pause())")
    s.move(700, 150, 0.2)
    tl.start()
    cards = s.page.locator(".teaser-card")
    tl.point(w(name, "كتب", 12.0, 12.6), "#request", fx=0.8)
    tl.point(w(name, "generate", 13.0, 14.6), "#go")
    tl.point(w(name, "ثلاث", 16.0, 16.8), cards.nth(0))
    tl.point(w(name, "حقيقي", 18.0, 18.8), cards.nth(1))
    t_cur = w(name, "شايفين", 20.0, 21.6)
    tl.point(t_cur, cards.nth(2), fx=0.27, fy=0.42)
    tl.do(t_cur, lambda: s.page.evaluate(
        "() => { const v = document.querySelectorAll('.teaser-pair video');"
        " v.forEach(x => { x.currentTime = 0; }); v[0].play(); }"))  # fmt: skip
    t_fix = w(name, "عدل", 31.0, 32.2)
    tl.point(t_fix, cards.nth(2), fx=0.75, fy=0.42)
    tl.do(t_fix, lambda: s.page.evaluate(
        "() => { const v = document.querySelectorAll('.teaser-pair video'); v[1].currentTime = 0;"
        " v[1].play(); }"))  # fmt: skip
    tl.end(seg_len(name))


def rec_brain(s: Session, real: str, w: Words) -> None:
    name = "02_brain"
    tl = Timeline(s, name)
    s.goto(f"{real}/#run={V2}", settle=2.5)
    s.page.evaluate("document.querySelector('#decisions').scrollTop = 0")
    scroll_block(s, "#run .block")
    nodes = s.page.locator("#graph .node")
    node = {k: nodes.nth(i) for i, k in enumerate(
        ["brief", "concepts", "pick", "storyboard", "bible", "prompts", "keyframes", "clips", "qa",
         "assembly"])}  # fmt: skip
    tl.start()
    tl.point(0.3, "#graph", fy=0.3)
    tl.point(w(name, "يفكر", 15.0, 16.0), node["prompts"], fy=0.5)
    tl.point(w(name, "يتخطى", 18.0, 23.0), node["qa"])
    tl.point(w(name, "سلس", 28.0, 31.0), "#graph", fx=0.45, fy=0.55)
    tl.point(w(name, "LangGraph", 40.0, 44.0), "#graph path.loop")
    tl.point(w(name, "brief", 50.0, 51.0), node["brief"])
    tl.point(w(name, "concept", 88.0, 89.5), node["concepts"])
    tl.point(w(name, "الاختيار", 96.0, 100.0), node["pick"])
    tl.point(w(name, "بوابة", 110.0, 113.0), s.page.locator("#graph rect.gate").nth(0))
    tl.point(w(name, "storyboard", 120.0, 122.5), node["storyboard"])
    tl.point(w(name, "للمرجع", 134.0, 136.0), node["bible"])
    tl.point(w(name, "البرومت", 161.0, 162.5), node["prompts"])
    tl.point(w(name, "فريمز", 179.0, 180.0), node["keyframes"])
    # the locked packshot (Storyboard block's product reference card)
    t_ref = w(name, "مرجعية", 184.0, 186.5)
    tl.do(t_ref - 1.6, lambda: scroll_block(s, "#product-ref", offset=180))
    tl.point(t_ref, "#product-ref img")
    tl.point(w(name, "اسمه", 199.0, 202.0), "#product-ref .pr-text", fx=0.3)
    # the gate: back to the scene lanes, a rejected still
    t_gate = w(name, "سوينا", 204.5, 205.0)
    tl.do(t_gate - 1.4, lambda: scroll_block(s, "#lanes-wrap", offset=320))
    tl.point(t_gate, s.page.locator("#lanes .row").nth(0).locator(".step.gate.fail").first)
    tl.point(w(name, "اختلفت", 214.0, 215.5), s.page.locator("#lanes .row").nth(7).locator(
        ".step.gate.fail").first)  # fmt: skip
    tl.do(w(name, "نروح", 218.5, 219.5) - 1.3, lambda: scroll_block(s, "#run .block"))
    tl.point(w(name, "نروح", 218.5, 219.5), node["clips"])
    tl.point(w(name, "والمراجع", 229.5, 230.6), node["qa"])
    tl.point(w(name, "السيمبلي", 255.0, 256.6), node["assembly"])
    items = s.page.locator("#decisions li")
    tl.point(w(name, "اليمين", 264.5, 265.8), items.filter(has_text="Code picked").first, fx=0.3)
    tl.point(w(name, "الكود", 271.0, 272.9), items.filter(has_text="montage format").first,
             fx=0.3)  # fmt: skip
    tl.point(w(name, "الل|LLM|ال", 274.0, 274.8), s.page.locator("#decisions .who.llm").first)
    tl.end(seg_len(name))


def rec_chaos(s: Session, live: str, real: str, w: Words) -> str:
    name = "03_chaos_run"
    tl = Timeline(s, name)
    s.goto(f"{live}/", settle=3.0)
    s.page.fill("#request", REQUEST)
    s.move(800, 300, 0.2)
    tl.start()
    tl.point(0.2, "label.check:has(#chaos)")
    tl.do(0.9, lambda: s.click("label.check:has(#chaos)", pause=0.2))
    tl.do(1.6, lambda: s.click("#go", pause=0.2))
    s.page.wait_for_function("location.hash.includes('run=')")
    rid = str(s.page.evaluate("location.hash.split('run=')[1]"))
    tl.do(4.0, lambda: scroll_block(s, "#run .block"))
    rows = s.page.locator("#lanes .row")
    tl.point(w(name, "عندنا", 6.0, 6.6), "#lanes-wrap", fx=0.25, fy=0.3)
    # «الأول الثانية الثالث» come 0.6 s apart: spread over the sentence instead of a sweep
    lanes = "#lanes-wrap"
    tl.point(w(name, "الأول", 14.0, 15.0), rows.nth(0).locator(".step.gate").first, lanes)
    tl.point(w(name, "الكلب", 17.5, 18.4), rows.nth(1).locator(".step.gate").first, lanes)
    tl.point(w(name, "وراح|للبوابة", 21.0, 21.8), rows.nth(2).locator(".step.gate").first, lanes)
    tl.point(w(name, "الرابع", 26.0, 29.5), rows.nth(3).locator(".step.tag", has_text="retry"))
    tl.point(w(name, "اجتاز", 41.0, 42.3), rows.nth(3).locator(".step.end").first)
    tl.point(w(name, "الثالث", 50.0, 52.3), rows.nth(2).locator(".step.tag").nth(0))
    tl.point(w(name, "ريرايت|سوى", 57.0, 58.6), rows.nth(2).locator(".step.tag").nth(1))
    tl.point(w(name, "الثاني", 62.0, 64.6), rows.nth(1).locator(".step.qa.fail").first)
    # the correction to scene 5 is the real v2 ad: its lanes
    t_s5 = w(name, "الخامس", 72.0, 75.0)
    tl.do(t_s5 - 2.4, lambda: (s.goto(f"{real}/#run={V2}", settle=0.9),
                               scroll_block(s, "#lanes-wrap", offset=320)))  # fmt: skip
    v2rows = s.page.locator("#lanes .row")
    tl.point(t_s5, v2rows.nth(4).locator(".step.gate.fail").first)
    tl.point(w(name, "فيل", 86.0, 87.6), v2rows.nth(4).locator(".step.tag").first)
    tl.point(w(name, "ابروف", 92.0, 93.6), v2rows.nth(4).locator(".step.end").first)
    tl.end(seg_len(name))
    return rid


def rec_edit(s: Session, live: str, rid: str, w: Words) -> None:
    name = "04_edit"
    tl = Timeline(s, name)
    s.goto(f"{live}/#run={rid}", settle=2.5)
    scroll_block(s, "#board-wrap", offset=60)
    tl.start()
    tl.point(0.3, "#edit", fx=0.6)
    t_write = w(name, "سأكتب|اكتب", 3.0, 4.6)
    tl.do(t_write, lambda: s.click("#edit", pause=0.2))
    tl.do(t_write + 0.6, lambda: s.page.keyboard.type("خل المشهد الثاني أبطأ", delay=105))
    tl.do(w(name, "فالنظام", 8.0, 9.4), lambda: s.click("#edit-go", pause=0.2))
    tl.point(w(name, "هنا", 12.5, 12.9), "#blast", fx=0.35)
    tl.point(w(name, "فائدته", 30.0, 30.8), s.page.locator("#board .scene").nth(1), fy=0.55)
    tl.point(w(name, "فالمنتج", 39.5, 40.2), "#product-ref img")
    tl.point(w(name, "بقية", 46.5, 47.5), s.page.locator("#board .scene").nth(4), fy=0.55)
    tl.point(w(name, "توكينز|تستهلك", 50.0, 52.0), s.page.locator("#board .scene").nth(6),
             fy=0.55)  # fmt: skip
    tl.point(w(name, "صفر", 57.0, 58.5), s.page.locator("#board .scene").nth(1), fy=0.85)
    tl.end(seg_len(name))


def rec_real_runs(s: Session, real: str, w: Words) -> None:
    name = "05_real_runs"
    tl = Timeline(s, name)
    s.goto(f"{real}/#gallery", settle=2.5)
    card = s.page.locator(".g-card", has_text="curtain veil")
    card.evaluate("e => e.scrollIntoView({block: 'start'})")
    s.page.evaluate("window.scrollBy(0, -56)")
    s.page.evaluate(
        "() => document.querySelectorAll('video').forEach(v => { v.pause(); v.currentTime = 0; })"
    )
    s.move(500, 400, 0.2)
    vids = card.locator(".g-video")
    tl.start()
    tl.do(0.1, lambda: card.evaluate("c => c.querySelectorAll('video').forEach(v => v.play())"))
    tl.point(0.2, vids.nth(0), fy=0.45)
    tl.point(w(name, "رجع", 9.0, 9.6), vids.nth(1), fy=0.45)
    t_crit = w(name, "وهنا", 14.0, 14.8)
    tl.do(t_crit - 1.0, lambda: s.goto((DEMO / "lessons.html").as_uri(), settle=0.4))
    tl.point(t_crit, "#calibration h2", fx=0.3)
    tl.point(w(name, "متساهل", 23.0, 24.5), "#calibration .step.bad >> nth=0", fx=0.3)
    tl.point(w(name, "شددت", 33.0, 34.2), "#calibration .step.bad >> nth=1", fx=0.3)
    tl.point(w(name, "متوازن", 44.0, 46.5), "#calibration .step.good", fx=0.3)
    tl.point(w(name, "يرفض", 48.0, 49.5), "#calibration .big")
    tl.point(w(name, "النص", 52.5, 53.6), "#kurta img", fy=0.4)
    tl.point(w(name, "الثوب", 63.5, 64.0), "#kurta .lint", fx=0.3)
    tl.point(w(name, "فالموديل", 68.5, 69.2), "#kurta img", fy=0.4)
    tl.point(w(name, "سوعته|سويته|ضبت", 74.5, 76.0), "#kurta .fix", fx=0.3)
    tl.point(w(name, "الثالث", 87.5, 88.2), "#gate h2", fx=0.3)
    tl.point(w(name, "الصورة", 93.0, 93.6), "#gate img", fy=0.3)
    tl.point(w(name, "ضفتها", 104.0, 105.0), "#gate .big >> nth=0")
    tl.point(w(name, "مرة", 110.5, 111.2), "#gate .big >> nth=1")
    tl.end(seg_len(name))


def rec_resume_limits(s: Session, w: Words) -> None:
    name = "06_resume_limits"
    tl = Timeline(s, name)
    root = str(DEMO.parents[1]) + "/"
    data = json.loads((DEMO / "make_demo.json").read_text())
    data["frames"] = [[t, c.replace(root, "~/adsmith/")] for t, c in data["frames"]]
    lead = tl.lead
    t_kill = w(name, "بنقفل|نقفل", 18.0, 24.0)
    t_resume = w(name, "يكمل|بيرجع", 27.6, 28.6)
    t_table = w(name, "اكتملت", 35.0, 36.5)
    # capture markers of make demo: 3 · Kill 10.19 s, killed 15.18 s, resumed 15.34 s,
    # Crash / resume table 17.02 s
    anchors = [[0, 0.8], [10.186, 10.0], [15.18, t_kill + lead], [15.339, t_resume + lead],
               [17.016, t_table + lead], [17.017, t_table + lead + 0.1]]  # fmt: skip
    s.goto((DEMO / "terminal.html").as_uri(), settle=1.5)
    s.move(1560, 900, 0.2)
    tl.start()
    s.page.evaluate("([d, a]) => { window.playMapped(d, a); }", [data, anchors])
    t_lim = w(name, "الأمانة|والأمانة|كنسخة", 45.5, 46.8)
    tl.do(t_lim - 0.8, lambda: s.goto((DEMO / "limits.html").as_uri(), settle=0.2))
    tl.point(t_lim, "#l1 h2", fx=0.3)
    tl.point(w(name, "سوتس|eval|ما عندي", 57.0, 59.5), "#l2 h2", fx=0.3)
    tl.point(w(name, "بروسس|والنظام", 67.5, 68.3), "#l3 h2", fx=0.3)
    tl.point(w(name, "قرار", 72.0, 74.0), "#l3 p", fx=0.3)
    tl.end(seg_len(name))


# ---------------------------------------------------------------------------------- assembly
def cut_exact(src: Path, start: float, seconds: float, out: Path) -> None:
    """[start, start+seconds] of the session video, normalised (1920x1080, SAR 1:1, 30 fps,
    silent stereo track), padded by holding the last frame if the recording is short."""
    tmp = out.with_suffix(".raw.mp4")
    ffmpeg("-ss", f"{start:.3f}", "-i", str(src), "-vf",
           f"tpad=stop_mode=clone:stop_duration={seconds:.3f}", "-t", f"{seconds:.3f}",
           "-an", "-c:v", "libx264", "-crf", "16", str(tmp))  # fmt: skip
    normalise(tmp, out, seconds=seconds, keep_audio=False)
    tmp.unlink()


def hook_video(landing: Path, out: Path) -> None:
    v2 = DEMO.parents[1] / "docs" / "media" / "montage_v2.mp4"
    pad = out.with_suffix(".pad.mp4")
    ffmpeg("-i", str(v2), "-vf", "scale=-2:1080,pad=1920:1080:(ow-iw)/2:0:color=0x0e0f10",
           "-an", "-c:v", "libx264", "-crf", "16", str(pad))  # fmt: skip
    a = out.with_suffix(".a.mp4")
    normalise(pad, a, seconds=V2_CLIP, keep_audio=False)
    concat([a, landing], out)
    for p in (pad, a):
        p.unlink()


def main() -> None:  # noqa: PLR0915 - one branch per segment
    ap = argparse.ArgumentParser()
    ap.add_argument("--words", type=Path, required=True)
    ap.add_argument("--real", default="http://localhost:8793")
    ap.add_argument("--live", default="http://localhost:8792")
    ap.add_argument("--only", nargs="*", default=[])
    args = ap.parse_args()
    w = Words(args.words)
    SEG.mkdir(parents=True, exist_ok=True)
    tmp = DEMO / ".raw"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    plan = ["01_hook", "02_brain", "03_chaos_run", "04_edit", "05_real_runs", "06_resume_limits"]
    todo = [p for p in plan if not args.only or p in args.only]
    recorded: list[tuple[str, Path, float]] = []
    rid = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        for name in todo:
            ctx, s = new_session(browser, tmp)
            print(f"recording {name} ({seg_len(name):.1f}s)")
            if name == "01_hook":
                rec_hook(s, args.real, w)
            elif name == "02_brain":
                rec_brain(s, args.real, w)
            elif name == "03_chaos_run":
                rid = rec_chaos(s, args.live, args.real, w)
            elif name == "04_edit":
                if not rid:
                    runs = json.loads(__import__("urllib.request").request.urlopen(
                        f"{args.live}/api/runs").read())  # fmt: skip
                    rid = next(r["id"] for r in runs if r["stage"] in {"done", "done_partial"})
                rec_edit(s, args.live, rid, w)
            elif name == "05_real_runs":
                rec_real_runs(s, args.real, w)
            else:
                rec_resume_limits(s, w)
            video = s.page.video
            ctx.close()
            assert video is not None
            start, end = s.marks[name]
            recorded.append((name, Path(video.path()), start))
            print(f"  recorded {end - start:.1f}s")
        browser.close()
    for name, src, start in recorded:
        out = SEG / f"{name}.mp4"
        if name == "01_hook":
            landing = SEG / "01_hook.landing.mp4"
            cut_exact(src, start, seg_len(name) - V2_CLIP, landing)
            hook_video(landing, out)
            landing.unlink()
        else:
            cut_exact(src, start, seg_len(name), out)
        print(f"{out.name}: {duration(out):.2f}s (voice {voice_len(name):.2f}s)")
    shutil.rmtree(tmp, ignore_errors=True)
    files = [SEG / f"{n}.mp4" for n in plan]
    if all(f.exists() for f in files):
        draft = DEMO / "draft_silent.mp4"
        concat(files, draft)
        print(draft, f"{duration(draft):.2f}s")
        print(profile(draft))


if __name__ == "__main__":
    main()
