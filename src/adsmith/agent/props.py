"""PROPS: one reference image per prop the whole-ad prompt names (ADR-0026, phase 3).

Try 1 showed what happens without one: the hostess held a red copy of the product instead of an
incense burner, because only the product photo was sent. Try 2 fixed it with a hand-made burner
image. This stage does that by code, between the director and the estimate:

- for each prop reference of the prompt (at most 3) that no reference image already covers (a
  user ``--ref-image`` with the same name wins), text-to-image on the default image model;
- a vision check against the product photo: the described prop, wordless, and NOT resembling
  the product (a product issue, hard by code); one retry with the critic's hint, then dropped;
- accepted images join ``reference_images`` after the user's, and the prompt is re-rendered so
  the References line numbers them; if the text no longer fits the model's limit, generated
  props are dropped last-first until it does.

Paid images are reserved, settled or released like the product reference; a run cap
(``props_max_usd``) refuses before any call. Every failure is an event; the stage never fails the
run (the prompt still names each prop with its description).
"""

from __future__ import annotations

import functools
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from adsmith.agent.context import ad_endpoint
from adsmith.agent.media import MediaGateway, PreparedJob
from adsmith.agent.prompts import PromptLibrary
from adsmith.agent.recovery.backoff import Sleep, retry_transient
from adsmith.agent.recovery.errors import AccountLockedError, AgentError
from adsmith.config import PROJECT_ROOT, Settings
from adsmith.domain.ad import ReferenceSpec, earns_image, fit_references, ref_key
from adsmith.domain.base import stable_hash
from adsmith.domain.budget import BudgetRefusedError
from adsmith.domain.culture_bank import GulfObject
from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.generation import Artifact
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAVerdict
from adsmith.domain.run import PropReference, RunState
from adsmith.storage import Storage

MAX_PROPS = 3
TRIES_PER_PROP = 2
PROP_SIDE = 768


class PropCritic(Protocol):
    async def review_prop_reference(
        self, state: RunState, image: Artifact, spec: ReferenceSpec, prompt: str
    ) -> tuple[QAVerdict, list[LLMCallRecord]]: ...


def _ev(
    run_id: str, type_: EventType, summary: str, level: str = "info", **payload: object
) -> AgentEvent:
    return AgentEvent.model_validate(
        {
            "run_id": run_id,
            "stage": Stage.PROPS,
            "type": type_,
            "summary": summary[:500],
            "payload": payload,
            "level": level,
        }
    )


def wanted_props(state: RunState) -> list[ReferenceSpec]:
    """The prompt's prop references that earn an image (``earns_image``) and that no reference
    image covers yet (by name)."""
    g = state.generation
    if g is None or g.human_prompt is not None:
        return []
    have = {ref_key(r.name) for r in state.reference_images}
    props = [r for r in g.prompt.references if earns_image(r)][:MAX_PROPS]
    return [r for r in props if ref_key(r.name) not in have]


def text_only_props(state: RunState) -> list[ReferenceSpec]:
    g = state.generation
    if g is None:
        return []
    return [r for r in g.prompt.references if r.role == "prop" and not earns_image(r)]


@dataclass
class PropsRunner:
    storage: Storage
    gateway: MediaGateway
    critic: PropCritic
    prompts: PromptLibrary
    settings: Settings
    sleep: Sleep
    rng: random.Random

    def _note(self, run_id: str, summary: str, level: str = "info", **payload: object) -> None:
        self.storage.events.append(run_id, [_ev(run_id, EventType.DECISION, summary, level,
                                                **payload)])  # fmt: skip

    def _scratch(self, run_id: str) -> Path:
        d = self.storage.artifacts.run_dir(run_id) / "tmp" / "props"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def run(self, run_id: str) -> None:
        state = self.storage.runs.load(run_id)
        todo = wanted_props(state)
        if skipped := text_only_props(state):
            names = ", ".join(r.name for r in skipped)
            self._note(
                run_id,
                f"Props: {names} stay in the shot text only (not handled, in one shot, small, "
                "not a look-alike): no image, no References line",
            )
        if not todo:
            self._note(run_id, "Props: no prop image to generate")
            self._rerender(run_id)
            return
        from_library = [r.name for r in todo if self._library_object(r) is not None]
        to_generate = [r.name for r in todo if r.name not in from_library]
        parts = []
        if from_library:
            parts.append(f"from the library: {', '.join(from_library)}")
        if to_generate:
            parts.append(f"to generate: {', '.join(to_generate)}")
        self._note(run_id, "Props: reference images " + "; ".join(parts))
        for index, spec in enumerate(todo, start=1):
            if await self._one(run_id, spec, index) == "stop":
                break
        self._rerender(run_id)

    def _prompt(self, state: RunState, spec: ReferenceSpec, hint: str) -> str:
        category = state.brief.product_category if state.brief else "perfume"
        obj = self.settings.culture_bank.object_for(spec.name)
        return (
            self.prompts.get("prop_reference")
            .render(
                name=spec.name,
                description=obj.description if obj else spec.description,
                not_like="; ".join(obj.not_like) if obj else "",
                category=category,
                transforms=spec.transforms_into_product,
                hint=hint,
            )
            .user
        )

    async def _one(self, run_id: str, spec: ReferenceSpec, index: int) -> str:
        obj = self._library_object(spec)
        if obj is not None and await self._from_library(run_id, spec, obj) == "accepted":
            return "accepted"
        hint = ""
        for attempt in range(1, TRIES_PER_PROP + 1):
            state = self.storage.runs.load(run_id)
            prompt = self._prompt(state, spec, hint)
            prepared = self.gateway.prepare_prop(prompt, index=index, attempt_no=attempt,
                                                 side=PROP_SIDE)  # fmt: skip
            outcome = await self._try(run_id, spec, prompt, prepared, attempt)
            if isinstance(outcome, str) and outcome in ("accepted", "stop"):
                return outcome
            if isinstance(outcome, QAVerdict):
                hint = outcome.revised_prompt_hint or ""
        self._note(
            run_id,
            f"Props: no acceptable image for the {spec.name} in {TRIES_PER_PROP} tries; it is "
            "dropped (the prompt still describes it in words)",
            level="warn",
        )
        return "dropped"

    async def _try(
        self,
        run_id: str,
        spec: ReferenceSpec,
        prompt: str,
        prepared: PreparedJob,
        attempt: int,
    ) -> QAVerdict | str:
        ref = f"prop_{ref_key(spec.name).replace(' ', '_')}_t{attempt}"
        cap = self.settings.props_max_usd
        state = self.storage.runs.load(run_id)
        spent = sum(r.cost_usd for r in state.reference_images if r.source == "generated")
        if spent + prepared.est_usd > cap + 1e-9:
            self._note(
                run_id,
                f"Props: the {spec.name} would cost ${prepared.est_usd:.3f}; with ${spent:.3f} "
                f"already spent on props it is above the ${cap:.2f} props cap: stopped",
                level="warn",
            )
            return "stop"

        def reserve(s: RunState) -> list[AgentEvent]:
            s.budget.reserve(ref, "image", prepared.est_usd, None)
            return [_ev(run_id, EventType.ATTEMPT_STARTED,
                        f"Props: generating the {spec.name} (try {attempt}/{TRIES_PER_PROP}, "
                        f"${prepared.est_usd:.3f})", est_usd=prepared.est_usd)]  # fmt: skip

        def release(s: RunState) -> list[AgentEvent]:
            s.budget.release(ref)
            return []

        try:
            self.storage.runs.update(run_id, reserve, reason=f"prop {spec.name}")
        except BudgetRefusedError as exc:
            self._note(run_id, f"Props: refused by the budget: {exc}", level="warn")
            return "stop"
        try:
            res = await retry_transient(
                functools.partial(self.gateway.run, prepared, self._scratch(run_id)),
                sleep=self.sleep, rng=self.rng,
            )  # fmt: skip
        except AccountLockedError:
            self.storage.runs.update(run_id, release, reason="prop paused")
            raise
        except AgentError as exc:
            self.storage.runs.update(run_id, release, reason="prop failed")
            self._note(run_id, f"Props: the {spec.name} try {attempt} failed: {exc.message}",
                       level="warn")  # fmt: skip
            return "failed"
        state = self.storage.runs.load(run_id)
        verdict: QAVerdict | None
        try:
            verdict, calls = await self.critic.review_prop_reference(
                state, res.artifact, spec, prompt
            )
        except AgentError as exc:
            verdict, calls = None, exc.llm_calls
        accepted = verdict is not None and verdict.passed

        def record(s: RunState) -> list[AgentEvent]:
            s.budget.settle(ref, res.cost_usd)
            events: list[AgentEvent] = []
            for c in calls:
                s.budget.record_actual("llm", c.cost_usd, ref=c.id, scene_id=None)
                events.append(c.as_event(s.id, Stage.PROPS, None))
            if accepted and verdict is not None:
                s.reference_images.append(
                    PropReference(
                        artifact=res.artifact, name=spec.name, source="generated",
                        cost_usd=round(res.cost_usd, 6), check=f"passed {verdict.score:.2f}",
                    )
                )  # fmt: skip
                msg = (
                    f"Props: the {spec.name} image passed the check (score {verdict.score:.2f}, "
                    f"${res.cost_usd:.3f}{', cached' if res.cache_hit else ''})"
                )
                events.append(_ev(run_id, EventType.DECISION, msg, image=res.artifact.path,
                                  score=verdict.score, cost_usd=res.cost_usd))  # fmt: skip
            else:
                why = "; ".join(verdict.fail_reasons) if verdict else "the check did not answer"
                issues = [i.observation for i in verdict.issues] if verdict else []
                events.append(_ev(run_id, EventType.DECISION,
                                  f"Props: the {spec.name} image was rejected ({why})",
                                  level="warn", issues=issues))  # fmt: skip
            return events

        self.storage.runs.update(run_id, record, reason=f"prop {spec.name} reviewed")
        if accepted:
            return "accepted"
        self.storage.cache.evict(prepared.request.cache_key())
        return verdict if verdict is not None else "failed"

    def _library_object(self, spec: ReferenceSpec) -> GulfObject | None:
        """The Gulf object whose verified photo this prop will use, or None (library off, no
        object, no photo, or the photo missing on disk)."""
        if not self.settings.props_library:
            return None
        obj = self.settings.culture_bank.object_for(spec.name)
        if obj is None or not obj.ref_image:
            return None
        path = Path(obj.ref_image)
        return obj if (path if path.is_absolute() else PROJECT_ROOT / path).is_file() else None

    async def _from_library(self, run_id: str, spec: ReferenceSpec, obj: GulfObject) -> str:
        """A verified photo of the Gulf object, at $0 instead of a generated image. It is checked
        once (the cheap vision check) and the pass is cached by the photo's hash, so later runs
        use it without a call. A missing or rejected photo falls back to generation."""
        assert obj.ref_image is not None
        path = Path(obj.ref_image)
        path = path if path.is_absolute() else PROJECT_ROOT / path
        if not path.is_file():
            self._note(run_id, f"Props: library photo for the {spec.name} is missing ({path}); "
                       "generating one instead", level="warn")  # fmt: skip
            return "missing"
        art = self.storage.artifacts.ingest(path, kind="image", provider="library")
        key = stable_hash("props.library.check.v1", [art.sha256, obj.id, obj.description])
        cached = self.storage.cache.get(key) is not None
        calls: list[LLMCallRecord] = []
        verdict: QAVerdict | None = None
        if not cached:
            state = self.storage.runs.load(run_id)
            try:
                verdict, calls = await self.critic.review_prop_reference(
                    state, art, spec, f"library photo: {obj.description}"
                )
            except AgentError as exc:
                verdict, calls = None, exc.llm_calls
        passed = cached or (verdict is not None and verdict.passed)
        check = "cached pass" if cached else (
            f"passed {verdict.score:.2f}" if verdict is not None and verdict.passed else "rejected"
        )  # fmt: skip

        def record(s: RunState) -> list[AgentEvent]:
            events: list[AgentEvent] = []
            for c in calls:
                s.budget.record_actual("llm", c.cost_usd, ref=c.id, scene_id=None)
                events.append(c.as_event(s.id, Stage.PROPS, None))
            if passed:
                s.reference_images.append(
                    PropReference(artifact=art, name=spec.name, source="library",
                                  cost_usd=0.0, check=f"library, {check}")
                )  # fmt: skip
                events.append(_ev(
                    run_id, EventType.DECISION,
                    f"Props: prop from library: the {spec.name} ({obj.ref_image}), $0, check "
                    f"{check}", image=art.path, library=obj.ref_image,
                ))  # fmt: skip
            else:
                issues = [i.observation for i in verdict.issues] if verdict else []
                events.append(_ev(
                    run_id, EventType.DECISION,
                    f"Props: the library photo of the {spec.name} was rejected by the check; "
                    "generating one instead", level="warn", issues=issues,
                ))  # fmt: skip
            return events

        self.storage.runs.update(run_id, record, reason=f"prop {spec.name} from library")
        if passed and not cached:
            self.storage.cache.put(key, art)
        return "accepted" if passed else "rejected"

    def _rerender(self, run_id: str) -> None:
        """Number the references in the prompt from the images that now exist, and fit the text
        within the model's limit (``fit_references``): compact prop lines first, then drop
        generated props, then library props, each drop a warning."""
        max_chars = ad_endpoint(
            self.settings, self.storage.runs.load(run_id).config.ad_model
        ).max_prompt_chars
        bank = self.settings.culture_bank

        def mutate(s: RunState) -> list[AgentEvent]:
            g = s.generation
            if g is None or g.human_prompt is not None:
                return []
            events: list[AgentEvent] = []
            refs = [(r.name, r.source) for r in s.reference_images]
            text, dropped, compact = fit_references(
                g.prompt, refs, duration_s=g.prompt.duration_s, aspect_ratio=g.aspect_ratio,
                max_chars=max_chars, glosses=bank.glosses([r[0] for r in refs]),
            )  # fmt: skip
            if compact:
                events.append(_ev(
                    s.id, EventType.DECISION,
                    f"Props: the full References line is over the model's {max_chars}-character "
                    "limit; each prop line is shortened to its name and a gloss (the image "
                    "carries the look)",
                ))  # fmt: skip
            for i in sorted(dropped, reverse=True):
                gone = s.reference_images.pop(i)
                events.append(_ev(
                    s.id, EventType.DECISION,
                    f"Props: the {gone.name} image ({gone.source}) is dropped: even compact, the "
                    f"prompt is over the model's {max_chars}-character limit", level="warn",
                ))  # fmt: skip
            if text != g.prompt.text:
                s.generation = g.model_copy(
                    deep=True, update={"prompt": g.prompt.model_copy(update={"text": text})}
                )
                images = ["Image 1: the product photo"] + [
                    f"Image {i}: {r.name} ({r.source})"
                    for i, r in enumerate(s.reference_images, start=2)
                ]
                events.append(_ev(s.id, EventType.DECISION,
                                  "Props: references renumbered: " + "; ".join(images)
                                  + f" ({len(text)} characters)", images=images))  # fmt: skip
            return events

        self.storage.runs.update(run_id, mutate, reason="props: prompt re-rendered")
