"""STOP 1 contact sheet: the locked product reference and every scene's keyframe, with what the
system decided (vocabulary entry, identity mode, gate score, tries) under each one.

    uv run python scripts/contact_sheet.py <run_id>      # → data/runs/<run>/contact_sheet.png

Rejected stills are shown as small thumbnails under each tile; a scene with no keyframe (refused
by the budget, or failed) gets a grey tile with the reason.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from adsmith.agent.context import AgentRuntime
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.domain.events import EventType
from adsmith.domain.generation import Artifact
from adsmith.domain.run import RunState
from adsmith.media.endcard import font

TILE_W, TILE_H = 270, 480
CAPTION_H, THUMB_H = 92, 96
COLS, PAD = 5, 14
BG, INK, MUTED, OK, BAD = (
    (18, 18, 20),
    (235, 235, 235),
    (150, 150, 155),
    (90, 200, 120),
    (230, 90, 80),
)


@dataclass
class Tile:
    title: str
    lines: list[str]
    image: Artifact | None
    passed: bool | None
    rejected: list[Artifact]
    empty_reason: str = ""


def identities(rt: AgentRuntime, run_id: str) -> dict[str, str]:
    """attempt id → identity mode, read from the keyframe attempt events (the trace)."""
    out: dict[str, str] = {}
    for e in rt.storage.events.tail(run_id, limit=100_000):
        if e.type is EventType.ATTEMPT_STARTED and "identity" in e.payload:
            out[str(e.payload.get("attempt_id"))] = str(e.payload["identity"])
    return out


def tiles(rt: AgentRuntime, st: RunState) -> list[Tile]:
    out: list[Tile] = []
    pr = st.product_reference
    if pr is not None:
        v = pr.verdict
        out.append(
            Tile(
                title="REFERENCE packshot",
                lines=[
                    f"gate {v.score:.2f} {'PASS' if v.passed else 'FAIL'}" if v else "not judged",
                    f"tries {pr.tries} · ${pr.cost_usd:.3f}",
                ],
                image=pr.artifact or (pr.rejected[-1] if pr.rejected else None),
                passed=pr.approved,
                rejected=list(pr.rejected),
            )
        )
    ids = identities(rt, st.id)
    assert st.storyboard is not None
    for sc in st.storyboard.scenes:
        kfs = [
            a
            for a in sc.attempts
            if a.phase is ScenePhase.KEYFRAME and a.scene_version == sc.version and a.artifact
        ]
        last = kfs[-1] if kfs else None
        shown = sc.keyframe or (last.artifact if last else None)
        shown_att = next((a for a in kfs if a.artifact == shown), last)
        score = shown_att.qa_score if shown_att else None
        passed = (
            sc.keyframe_approved if sc.keyframe else (shown_att.qa_passed if shown_att else None)
        )
        ident = ids.get(shown_att.id, "?") if shown_att else "-"
        rejected = [a.artifact for a in kfs if a.qa_passed is False and a.artifact != shown]
        reasons = sc.keyframe_qa.fail_reasons if sc.keyframe_qa and not passed else []
        lines = [
            f"{sc.vocab or sc.purpose.value} · {ident} · {sc.duration_s:g}s",
            f"gate {score:.2f} {'PASS' if passed else 'FAIL'} · tries {len(kfs)}"
            if score is not None
            else f"tries {len(kfs)} · not judged",
        ]
        if reasons:
            lines.append(reasons[0][:44])
        empty = ""
        if shown is None:
            empty = sc.failure.reason_debug[:120] if sc.failure else f"{sc.status.value}"
        out.append(Tile(f"S{sc.order}", lines, shown, passed, [r for r in rejected if r], empty))
    return out


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    img = img.convert("RGB")
    img.thumbnail((w, h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (w, h), (8, 8, 9))
    canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    return canvas


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return [*lines, cur] if cur else lines


def render(rt: AgentRuntime, st: RunState, out: Path) -> Path:
    ts = tiles(rt, st)
    rows = (len(ts) + COLS - 1) // COLS
    cell_h = TILE_H + CAPTION_H + THUMB_H
    head = 70
    sheet = Image.new("RGB", (PAD + COLS * (TILE_W + PAD), head + rows * (cell_h + PAD)), BG)
    d = ImageDraw.Draw(sheet)
    f_title, f_text, f_small = font(24), font(17), font(14)
    concept = st.concepts.selected.title if st.concepts and st.concepts.selected else "-"
    fmt = st.storyboard.format.value if st.storyboard else "-"
    d.text((PAD, 12), f"{st.id} · format {fmt} · concept «{concept}»", font=f_title, fill=INK)
    d.text(
        (PAD, 44),
        f"stage {st.stage.value} · spent image ${st.budget.by_category.get('image', 0):.3f} "
        f"· llm ${st.budget.by_category.get('llm', 0):.3f}",
        font=f_small,
        fill=MUTED,
    )
    for i, t in enumerate(ts):
        x = PAD + (i % COLS) * (TILE_W + PAD)
        y = head + (i // COLS) * (cell_h + PAD)
        if t.image is not None:
            sheet.paste(
                _fit(Image.open(rt.storage.artifacts.resolve(t.image)), TILE_W, TILE_H), (x, y)
            )
        else:
            d.rectangle((x, y, x + TILE_W, y + TILE_H), fill=(60, 60, 64))
            for n, line in enumerate(_wrap(f"no keyframe: {t.empty_reason}", 30)[:8]):
                d.text((x + 10, y + 12 + n * 20), line, font=f_small, fill=INK)
        color = OK if t.passed else BAD if t.passed is False else MUTED
        d.rectangle((x, y, x + TILE_W - 1, y + TILE_H - 1), outline=color, width=3)
        d.text((x, y + TILE_H + 6), t.title, font=f_text, fill=color)
        for n, line in enumerate(t.lines[:3]):
            d.text((x, y + TILE_H + 30 + n * 19), line, font=f_small, fill=INK)
        tx = x
        for r in t.rejected[:4]:
            thumb = _fit(Image.open(rt.storage.artifacts.resolve(r)), 54, THUMB_H - 10)
            sheet.paste(thumb, (tx, y + TILE_H + CAPTION_H))
            d.rectangle(
                (tx, y + TILE_H + CAPTION_H, tx + 53, y + TILE_H + CAPTION_H + THUMB_H - 11),
                outline=BAD,
            )
            tx += 60
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def build(rt: AgentRuntime, run_id: str) -> Path:
    st = rt.storage.runs.load(run_id)
    return render(rt, st, rt.storage.artifacts.run_dir(run_id) / "contact_sheet.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    args = ap.parse_args()
    print(build(AgentRuntime.from_settings(Settings()), args.run))


if __name__ == "__main__":
    main()
