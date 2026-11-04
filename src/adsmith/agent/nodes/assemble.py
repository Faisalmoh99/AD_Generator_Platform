"""assemble: approved clips → one MP4 with an end card (M8).

1. Each approved clip is trimmed to its scene duration and normalised (size, SAR, fps, pixel
   format; audio dropped). Failed scenes are skipped (default, ``partial_mode=skip``) or replaced by
   a "scene unavailable" slate of the same length (``slate``), so the run ends ``done_partial``
   with a watchable ad and a clear list of what failed.
2. End card (``endcard_s`` seconds): the brand name and tagline fade in over a blurred, darkened
   copy of the ad's last frame. The copy comes from the brief; if it has none, the
   ``endcard_copy`` LLM proposes a FICTIONAL brand and tagline, flagged as a placeholder in the
   trace and the Production Pack (never a real brand: deterministic denylist + repair). Once
   chosen, the copy is kept in run state, so re-assembly after an edit shows the same brand.
3. Concat; optional user-supplied music (looped, trimmed, faded out); then the duration is checked
   with ffprobe against the expected total.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import Delta, SetEndCard, SetFinal
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import brief_block, concept_block, decision, language_name
from adsmith.config import LLMNode
from adsmith.domain.endcard import EndCardCopy, EndCardCopyDraft, real_brand_in
from adsmith.domain.enums import Language, SceneStatus, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.run import RunState
from adsmith.media import ffmpeg
from adsmith.media.endcard import render_endcard, render_slate, style_from_palette
from adsmith.providers.media.base import frame_size

DURATION_TOLERANCE_S = 0.15
_SLATE_TEXT = {"ar": "المشهد {n} غير متوفر", "en": "Scene {n} unavailable"}


async def endcard_copy(
    state: RunState, ctx: NodeContext
) -> tuple[EndCardCopy, list[LLMCallRecord]]:
    """The brief's brand/tagline, completed by an invented placeholder only where missing."""
    if state.endcard is not None:
        return state.endcard, []
    assert state.brief is not None
    b = state.brief
    lang = b.tagline_language or b.language
    if b.brand_name and b.tagline:
        return EndCardCopy(brand_name=b.brand_name, tagline=b.tagline, language=lang), []
    concept = state.concepts.selected if state.concepts else None
    prompt = ctx.prompts.get("endcard_copy").render(
        brief=brief_block(b),
        concept=concept_block(concept) if concept else "(none)",
        given_brand=b.brand_name or "(none: invent one)",
        given_tagline=b.tagline or "(none: invent one)",
        user_language=language_name(b.language),
    )

    def convert(d: EndCardCopyDraft) -> EndCardCopy:
        brand = b.brand_name or d.brand_name
        tagline = b.tagline or d.tagline
        for label, text in (("brand_name", d.brand_name), ("tagline", d.tagline)):
            if real := real_brand_in(text):
                raise ValueError(f"{label} uses the real brand {real!r}: invent a fictional one")
        return EndCardCopy(
            brand_name=brand,
            tagline=tagline,
            language=b.language,
            brand_placeholder=not b.brand_name,
            tagline_placeholder=not b.tagline,
        )

    res = await ctx.llm.call_and_convert(LLMNode.ENDCARD_COPY, prompt, EndCardCopyDraft, convert)
    return res.value, res.calls


async def assemble(state: RunState, ctx: NodeContext) -> NodeResult:  # noqa: PLR0915
    assert state.storyboard is not None and state.brief is not None
    scenes = state.storyboard.scenes
    approved = [s for s in scenes if s.status is SceneStatus.APPROVED and s.clip]
    failed = [s for s in scenes if s.status is SceneStatus.FAILED]
    if not approved:
        orders = [s.order for s in failed]
        return NodeResult(
            deltas=[SetFinal(final=None)],
            events=[
                decision(ctx.run_id, Stage.ASSEMBLE, f"Nothing to assemble: scenes {orders} failed")
            ],
        )
    cfg = state.config
    long_side = cfg.media_long_side or ctx.settings.media_long_side  # the run's size
    w, h = frame_size(state.brief.aspect_ratio, long_side=long_side)
    work: Path = ctx.artifacts.scratch_dir(ctx.run_id) / "assemble"
    await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
    lang = "ar" if state.brief.language is Language.AR else "en"

    parts: list[Path] = []
    expected = 0.0
    slated: list[int] = []
    for s in scenes:
        if s.status is SceneStatus.APPROVED and s.clip is not None:
            seg = work / f"seg_{s.order}.mp4"
            await ffmpeg.normalise_clip(
                ctx.artifacts.resolve(s.clip), seg, seconds=s.duration_s, width=w, height=h
            )
        elif s.status is SceneStatus.FAILED and cfg.partial_mode == "slate":
            png = await asyncio.to_thread(
                render_slate, work / f"slate_{s.order}.png",
                message=_SLATE_TEXT[lang].format(n=s.order), width=w, height=h,
            )  # fmt: skip
            seg = await ffmpeg.still_clip(
                png, work / f"slate_{s.order}.mp4", seconds=s.duration_s, width=w, height=h
            )
            slated.append(s.order)
        else:
            continue
        parts.append(seg)
        expected += s.duration_s

    deltas: list[Delta] = []
    events: list[AgentEvent] = []
    calls: list[LLMCallRecord] = []
    copy: EndCardCopy | None = None
    if cfg.endcard and cfg.endcard_s > 0:
        copy, calls = await endcard_copy(state, ctx)
        deltas.append(SetEndCard(endcard=copy))
        palette = list(state.bible.color_grade.palette) if state.bible else []
        last = await ffmpeg.last_frame(parts[-1], work / "last_frame.png")
        bg = work / "endcard_bg.png"
        card = work / "endcard.png"
        style = style_from_palette(palette)
        await asyncio.to_thread(
            render_endcard, bg, brand=" ", tagline=" ", width=w, height=h, style=style,
            background_frame=last, text=False,
        )  # fmt: skip
        await asyncio.to_thread(
            render_endcard, card, brand=copy.brand_name, tagline=copy.tagline, width=w, height=h,
            style=style, background_frame=last,
        )  # fmt: skip
        parts.append(
            await ffmpeg.crossfade_stills(
                bg,
                card,
                work / "endcard.mp4",
                seconds=cfg.endcard_s,
                fade_s=0.5,
                width=w,
                height=h,
            )
        )
        expected += cfg.endcard_s
        tag = " [PLACEHOLDER: invented by the agent, replace before publishing]"
        events.append(
            decision(
                ctx.run_id,
                Stage.ASSEMBLE,
                f"End card: {copy.brand_name} — {copy.tagline}"
                + (tag if copy.is_placeholder else " (from the brief)"),
                brand=copy.brand_name,
                tagline=copy.tagline,
                placeholder=copy.is_placeholder,
            )
        )

    out = ctx.artifacts.run_dir(ctx.run_id) / "final.mp4"
    video_only = work / "video_only.mp4"
    await ffmpeg.concat(parts, video_only if cfg.music_path else out, work)
    if cfg.music_path:
        music = Path(cfg.music_path)
        if await asyncio.to_thread(music.is_file):
            await ffmpeg.add_music(video_only, music, out, seconds=expected)
        else:
            await asyncio.to_thread(video_only.replace, out)
            events.append(
                decision(ctx.run_id, Stage.ASSEMBLE, f"Music file not found, skipped: {music}")
            )
    actual = await ffmpeg.duration_of(out)
    ok = abs(actual - expected) <= DURATION_TOLERANCE_S
    events.append(
        AgentEvent(
            run_id=ctx.run_id,
            stage=Stage.ASSEMBLE,
            type=EventType.DECISION,
            level="info" if ok else "warn",
            summary=f"Duration check: {actual:.2f}s rendered vs {expected:.2f}s expected"
            + ("" if ok else f" (off by more than {DURATION_TOLERANCE_S}s)"),
            payload={"actual_s": round(actual, 3), "expected_s": round(expected, 3), "ok": ok},
        )
    )
    final = ctx.artifacts.ingest(
        out, kind="video", provider="ffmpeg", width=w, height=h, duration_s=round(actual, 3)
    )
    skipped = [s.order for s in failed if s.order not in slated]
    summary = f"Assembled {len(approved)} scene(s) + end card, {actual:.1f}s → final.mp4"
    if skipped:
        summary += f"; skipped failed scene(s) {skipped}"
    if slated:
        summary += f"; slate for failed scene(s) {slated}"
    events.insert(0, decision(ctx.run_id, Stage.ASSEMBLE, summary))
    deltas.append(SetFinal(final=final))
    return NodeResult(deltas=deltas, events=events, llm_calls=calls)
