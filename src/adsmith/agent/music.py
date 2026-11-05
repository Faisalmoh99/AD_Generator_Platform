"""Generated music for montage assembly (STOP 2). Not a pipeline stage: the orchestrator calls
``ensure_music`` when the generate stage starts (before any clip can use the budget) and again
before the assemble node; it runs once per run.

Order: user track configured → nothing to do; a track already in run state → reuse it; the cache
(same provider, model, prompt and seed) → $0; else reserve the run budget (category "audio", hard
cap ``music_max_usd``), one provider call with transient retries, settle, store in the CAS and in
run state. Every way it can fail (price above the cap, budget refused, provider error) ends in a
decision event and the ad is assembled without music: music never fails a run.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Literal

from adsmith.agent.context import AgentRuntime
from adsmith.agent.recovery.backoff import retry_transient
from adsmith.agent.recovery.errors import AgentError
from adsmith.domain.base import new_id
from adsmith.domain.budget import BudgetRefusedError
from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.formats import AdFormat
from adsmith.domain.generation import Artifact
from adsmith.domain.run import RunState
from adsmith.providers.media.base import MediaOutput, MusicProvider

MUSIC_SEED = 7


Level = Literal["info", "warn", "error"]


def _event(
    run_id: str,
    summary: str,
    level: Level = "info",
    stage: Stage = Stage.ASSEMBLE,
    **payload: object,
) -> AgentEvent:
    return AgentEvent.model_validate(
        {
            "run_id": run_id, "stage": stage, "type": EventType.DECISION,
            "summary": summary[:500], "level": level, "payload": payload,
        }
    )  # fmt: skip


def music_prompt(rt: AgentRuntime, state: RunState, seconds: float) -> str:
    assert state.brief is not None and state.storyboard is not None
    b = state.brief
    look = rt.settings.formats.grammar(state.storyboard.format).look
    return rt.prompts.get("music").render(
        seconds=f"{seconds:g}", product=b.product, audience=b.target_audience,
        tone=", ".join(b.tone), look=look,
    ).user.strip()  # fmt: skip


def cache_key(provider: MusicProvider, prompt: str, seed: int) -> str:
    blob = json.dumps(
        {"kind": "music", "provider": provider.name, "model": provider.model,
         "prompt": prompt, "seed": seed},
        sort_keys=True,
    )  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()


def wanted(state: RunState) -> bool:
    sb = state.storyboard
    return (
        state.config.generate_music
        and state.config.music_path is None
        and state.music is None
        and sb is not None
        and sb.format is AdFormat.MONTAGE
    )


async def ensure_music(rt: AgentRuntime, run_id: str) -> None:
    state = rt.storage.runs.load(run_id)
    if not wanted(state):
        return
    provider = rt.music_provider(state)
    if provider is None:
        _note(rt, run_id, "Music: no text-to-music model in the catalog; assembling without music")
        return
    cap = rt.settings.music_max_usd
    if provider.usd_per_track > cap + 1e-9:
        _note(
            rt, run_id,
            f"Music: {provider.model} costs ${provider.usd_per_track:.2f} per track, above the "
            f"${cap:.2f} music cap; assembling without music", level="warn",
        )  # fmt: skip
        return
    target_s = state.brief.duration_s if state.brief else 15.0
    seconds = round(target_s + state.config.endcard_s + 2.0)
    prompt = music_prompt(rt, state, seconds)
    key = cache_key(provider, prompt, MUSIC_SEED)
    hit = rt.storage.cache.get(key)
    if hit is not None:
        _store(rt, run_id, hit, None, f"Music: reused the cached {provider.model} track ($0)")
        return
    ref = new_id("mus")

    def reserve(s: RunState) -> list[AgentEvent]:
        s.budget.reserve(ref, "audio", provider.usd_per_track, None)
        return []

    try:
        rt.storage.runs.update(run_id, reserve, reason="music reserved")
    except BudgetRefusedError as exc:
        _note(rt, run_id, f"Music: refused by the budget ({exc}); assembling without music", "warn")
        return
    scratch = rt.storage.artifacts.scratch_dir(run_id)
    try:
        out: MediaOutput = await retry_transient(
            lambda: provider.generate_music(prompt, MUSIC_SEED, scratch, ref=f"{run_id} music"),
            sleep=rt.sleep, rng=random.Random(MUSIC_SEED),
        )  # fmt: skip
    except AgentError as exc:
        why = f"{exc.kind.value}: {exc.message[:200]}"

        def release(s: RunState) -> list[AgentEvent]:
            s.budget.release(ref)
            return [
                _event(
                    run_id,
                    f"Music generation failed ({why}); "
                    "assembling without music",
                    level="warn", stage=s.stage, model=provider.model,
                )
            ]  # fmt: skip

        rt.storage.runs.update(run_id, release, reason="music failed")
        return
    art = rt.storage.artifacts.ingest(
        out.path, kind="audio", provider=provider.name, duration_s=out.duration_s,
        remote_url=out.remote_url, move=True,
    )  # fmt: skip
    rt.storage.cache.put(key, art)
    _store(
        rt, run_id, art, (ref, out.cost_usd),
        f"Music: generated a {provider.track_s:g} s instrumental track with {provider.model} "
        f"(${out.cost_usd:.2f}, cap ${cap:.2f}); the ad is cut to its beats. "
        f"Prompt: {prompt[:220]}",
    )  # fmt: skip


def _store(
    rt: AgentRuntime, run_id: str, art: Artifact, settle: tuple[str, float] | None, summary: str
) -> None:
    def mutate(s: RunState) -> list[AgentEvent]:
        if settle is not None:
            s.budget.settle(*settle)
        s.music = art
        return [_event(run_id, summary, stage=s.stage, sha256=art.sha256)]

    rt.storage.runs.update(run_id, mutate, reason="music stored")


def _note(rt: AgentRuntime, run_id: str, summary: str, level: Level = "info") -> None:
    stage = rt.storage.runs.load(run_id).stage
    rt.storage.events.append(run_id, [_event(run_id, summary, level=level, stage=stage)])
