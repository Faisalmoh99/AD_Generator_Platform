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
3. Concat; music (a user-supplied track, else the run's generated one: agent/music.py) looped,
   trimmed and faded out; then the duration is checked with ffprobe against the expected total.
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
from adsmith.domain.formats import AdFormat
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.run import RunState
from adsmith.media import ffmpeg
from adsmith.media.beats import detect_beats, snap_cuts
from adsmith.media.endcard import render_endcard, render_slate, style_from_palette
from adsmith.providers.media.base import frame_size

DURATION_TOLERANCE_S = 0.15
BEAT_SLACK_S = 0.2
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
    if state.generation is not None:
        return await _assemble_whole(state, ctx)
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

    # A user track wins; else the generated one (agent/music.py, in run state).
    music_path = cfg.music_path or (
        str(ctx.artifacts.resolve(state.music)) if state.music is not None else None
    )
    windows, beat_events = await _beat_cut(state, music_path, cfg.partial_mode, ctx.run_id)
    parts: list[Path] = []
    expected = 0.0
    slated: list[int] = []
    for s in scenes:
        if s.status is SceneStatus.APPROVED and s.clip is not None:
            seg = work / f"seg_{s.order}.mp4"
            start, kept = windows.get(s.id, s.kept_window)
            await ffmpeg.normalise_clip(
                ctx.artifacts.resolve(s.clip), seg, seconds=kept, width=w, height=h, start_s=start
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
        expected += windows.get(s.id, (0.0, s.duration_s))[1]

    deltas: list[Delta] = []
    events: list[AgentEvent] = list(beat_events)
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
    await ffmpeg.concat(parts, video_only if music_path else out, work)
    if music_path:
        music = Path(music_path)
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


async def _beat_cut(
    state: RunState, music_path: str | None, partial_mode: str, run_id: str
) -> tuple[dict[str, tuple[float, float]], list[AgentEvent]]:
    """Montage + user music (PLAN_V2 item 6): shot boundaries snap to the nearest beat within
    ±0.2 s, the hero shot starts on a downbeat where one is in reach, and the end card starts on a
    beat. A shot's window may grow up to its rendered length (its start moves earlier if needed).
    Without music, or for other formats, the planned windows are used unchanged."""
    sb = state.storyboard
    if music_path is None or sb is None or sb.format is not AdFormat.MONTAGE:
        return {}, []
    music = Path(music_path)
    if not await asyncio.to_thread(music.is_file):
        return {}, []
    shots = [
        s
        for s in sb.scenes
        if (s.status is SceneStatus.APPROVED and s.clip is not None)
        or (s.status is SceneStatus.FAILED and partial_mode == "slate")
    ]
    if not shots:
        return {}, []
    grid = await detect_beats(music)
    planned = [s.kept_window[1] for s in shots]
    lo = [min(0.6, d) if s.clip else d for s, d in zip(shots, planned, strict=True)]
    hi = [
        (s.clip.duration_s or d) if s.clip is not None and s.use_window is not None else d
        for s, d in zip(shots, planned, strict=True)
    ]
    hero = len(shots) - 1 if shots[-1].shows_product else None
    new = snap_cuts(planned, grid, slack=BEAT_SLACK_S, hero_index=hero, min_s=lo, max_s=hi)
    windows: dict[str, tuple[float, float]] = {}
    for s, d in zip(shots, new, strict=True):
        start, _ = s.kept_window
        clip_s = s.clip.duration_s if s.clip is not None and s.clip.duration_s else d
        windows[s.id] = (round(max(0.0, min(start, clip_s - d)), 3), d)
    moved = sum(1 for a, b in zip(planned, new, strict=True) if abs(a - b) > 1e-3)
    summary = (
        f"Beat cut: {grid.bpm:g} bpm, {len(grid.beats)} beats; {moved} of {len(shots)} shot "
        f"lengths adjusted (≤ ±{BEAT_SLACK_S:g}s per cut); footage {sum(planned):.2f}s → "
        f"{sum(new):.2f}s"
    )
    return windows, [decision(run_id, Stage.ASSEMBLE, summary, bpm=grid.bpm, durations=list(new))]


async def _assemble_whole(state: RunState, ctx: NodeContext) -> NodeResult:
    """Multishot (ADR-0023): the whole generated ad + the end card, its native audio kept (the
    end card gets a silent track). No music generation and no beat cut: the model made the sound
    and the cuts. A failed or declined generation has nothing to assemble."""
    g = state.generation
    assert g is not None and state.brief is not None
    if g.artifact is None:
        why = g.failure.reason_user if g.failure else f"the generation is {g.status}"
        return NodeResult(
            deltas=[SetFinal(final=None)],
            events=[decision(ctx.run_id, Stage.ASSEMBLE, f"Nothing to assemble: {why}")],
        )
    cfg = state.config
    a = g.artifact
    if a.width and a.height:
        # The deliverable keeps the size that was paid for (720x1280 at 720p): no downscale.
        w, h = a.width // 2 * 2, a.height // 2 * 2
    else:
        long_side = cfg.media_long_side or ctx.settings.media_long_side
        w, h = frame_size(state.brief.aspect_ratio, long_side=long_side)
    work: Path = ctx.artifacts.scratch_dir(ctx.run_id) / "assemble"
    await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
    clip_s = min(float(g.duration_s), g.artifact.duration_s or float(g.duration_s))
    seg = await ffmpeg.normalise_clip_av(
        ctx.artifacts.resolve(g.artifact), work / "ad.mp4", seconds=clip_s, width=w, height=h
    )
    parts, expected = [seg], clip_s
    deltas: list[Delta] = []
    events: list[AgentEvent] = []
    calls: list[LLMCallRecord] = []
    if cfg.endcard and cfg.endcard_s > 0:
        copy, calls = await endcard_copy(state, ctx)
        deltas.append(SetEndCard(endcard=copy))
        palette = list(state.bible.color_grade.palette) if state.bible else []
        last = await ffmpeg.last_frame(seg, work / "last_frame.png")
        bg, card = work / "endcard_bg.png", work / "endcard.png"
        style = style_from_palette(palette)
        await asyncio.to_thread(
            render_endcard, bg, brand=" ", tagline=" ", width=w, height=h, style=style,
            background_frame=last, text=False,
        )  # fmt: skip
        await asyncio.to_thread(
            render_endcard, card, brand=copy.brand_name, tagline=copy.tagline, width=w, height=h,
            style=style, background_frame=last,
        )  # fmt: skip
        silent = await ffmpeg.crossfade_stills(
            bg, card, work / "endcard_v.mp4", seconds=cfg.endcard_s, fade_s=0.5, width=w, height=h
        )
        parts.append(await ffmpeg.with_silence(silent, work / "endcard.mp4", seconds=cfg.endcard_s))
        expected += cfg.endcard_s
        tag = " [PLACEHOLDER: invented by the agent, replace before publishing]"
        events.append(
            decision(
                ctx.run_id, Stage.ASSEMBLE,
                f"End card: {copy.brand_name} — {copy.tagline}"
                + (tag if copy.is_placeholder else " (from the brief)"),
                brand=copy.brand_name, tagline=copy.tagline, placeholder=copy.is_placeholder,
            )
        )  # fmt: skip
    out = ctx.artifacts.run_dir(ctx.run_id) / "final.mp4"
    await ffmpeg.concat(parts, out, work)
    actual = await ffmpeg.duration_of(out)
    ok = abs(actual - expected) <= DURATION_TOLERANCE_S
    events.append(
        AgentEvent(
            run_id=ctx.run_id, stage=Stage.ASSEMBLE, type=EventType.DECISION,
            level="info" if ok else "warn",
            summary=f"Duration check: {actual:.2f}s rendered vs {expected:.2f}s expected"
            + ("" if ok else f" (off by more than {DURATION_TOLERANCE_S}s)"),
            payload={"actual_s": round(actual, 3), "expected_s": round(expected, 3), "ok": ok},
        )
    )  # fmt: skip
    final = ctx.artifacts.ingest(
        out, kind="video", provider="ffmpeg", width=w, height=h, duration_s=round(actual, 3)
    )
    note = "" if g.status == "approved" else f" (the critic flagged it: {g.status}, warn only)"
    events.insert(
        0,
        decision(
            ctx.run_id, Stage.ASSEMBLE,
            f"Assembled the whole-ad generation + end card with its own audio, {actual:.1f}s → "
            f"final.mp4{note}",
        ),
    )  # fmt: skip
    deltas.append(SetFinal(final=final))
    return NodeResult(deltas=deltas, events=events, llm_calls=calls)
