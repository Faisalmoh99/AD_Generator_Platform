"""storyboard: selected concept → ordered scenes with shot specs. Durations must sum to the brief
and fit what the video model can render; violations feed the schema-repair loop."""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetStoryboard
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import (
    brief_block,
    concept_block,
    decision,
    flag_override_events,
    language_name,
    profile_block_for,
    scene_duration_bounds,
)
from adsmith.config import LLMNode
from adsmith.domain.enums import Stage
from adsmith.domain.formats import (
    AdFormat,
    format_block,
    grammar_problems,
    whole_ad_shot_count,
)
from adsmith.domain.run import RunState
from adsmith.domain.scene_flags import enforce_scene_flags, person_mentions
from adsmith.domain.shot_lint import multi_action_problem, product_presence_problem
from adsmith.domain.storyboard import Storyboard, StoryboardDraft
from adsmith.domain.vocabulary import VocabLibrary


async def storyboard(state: RunState, ctx: NodeContext) -> NodeResult:  # noqa: PLR0915
    assert state.brief is not None and state.concepts is not None
    concept = state.concepts.selected
    assert concept is not None
    video_model = state.config.video_model
    brief = state.brief
    fmt = brief.format
    grammar = ctx.settings.formats.grammar(fmt)
    min_s, max_s, single_max = scene_duration_bounds(ctx.settings, video_model, fmt)
    duration = brief.duration_s
    montage = fmt is AdFormat.MONTAGE
    whole = grammar.generation == "whole"  # multishot: the shots of ONE generation
    max_scenes = (
        min(grammar.max_shots, int(duration // min_s))
        if montage
        else whole_ad_shot_count(grammar, duration)
        if whole
        else max(2, min(6, int(duration // min_s)))
    )

    requested = state.brief.requested_shots if state.brief else None

    def convert(draft: StoryboardDraft) -> Storyboard:
        if requested is not None and len(draft.scenes) != requested:
            raise ValueError(
                f"the client asked for exactly {requested} shot(s); write exactly {requested} "
                f"scene(s), not {len(draft.scenes)} (keep the concept, split or merge its beats)"
            )
        if montage:
            return _montage(draft)
        if whole:
            return _whole(draft)
        single = len(draft.scenes) == 1
        if single and draft.scenes[0].duration_s > single_max:
            raise ValueError(
                f"a single take can last at most {single_max:g}s on this video model; split it "
                "into scenes or shorten it"
            )
        long_ = [
            f"{i}: {s.duration_s:g}s"
            for i, s in enumerate(draft.scenes, 1)
            if not single and s.duration_s > max_s
        ]
        if long_:
            raise ValueError(
                f"in a multi-scene ad each scene lasts at most {max_s:g}s: {', '.join(long_)}. "
                f"Use a single continuous take (up to {single_max:g}s) or redistribute the time."
            )
        short = [
            f"{i}: {s.duration_s:g}s" for i, s in enumerate(draft.scenes, 1) if s.duration_s < min_s
        ]
        if short:
            raise ValueError(
                f"scenes shorter than the video model's {min_s:g}s minimum: {', '.join(short)}. "
                f"Use at most {max_scenes} scenes and redistribute the time."
            )
        busy = [
            f"scene {i} subject_action {problem}"
            for i, s in enumerate(draft.scenes, 1)
            # A single take is ONE continuous causal chain by design (the format's strength):
            # the one-action-per-shot lint applies to multi-scene ads only.
            if not single and (problem := multi_action_problem(s.shot.subject_action))
        ]
        if product := _presence(draft):
            busy.append(product)
        if busy:
            raise ValueError("; ".join(busy))
        return Storyboard.from_draft(
            draft, concept_id=concept.id, target_duration_s=duration, fmt=fmt
        )

    def _presence(draft: StoryboardDraft) -> str | None:
        return product_presence_problem(
            [
                (
                    s.purpose.value,
                    s.shows_product,
                    f"{s.shot.subject_action} {s.shot.composition} {s.shot.setting}",
                )
                for s in draft.scenes
            ],
            brief.product,
        )

    vocab = ctx.settings.vocabulary.for_model(video_model)

    def _whole(draft: StoryboardDraft) -> Storyboard:
        """Multishot grammar (formats.toml): shot count, per-shot length, hero last; one beat per
        shot; the product seen before the hero. No vocabulary: one generation renders every shot
        from the real product photo, so there is no per-shot identity routing."""
        problems = grammar_problems(draft.scenes, grammar)
        if requested is None and len(draft.scenes) != max_scenes:
            mins = ", ".join(f"{k} {v:g} s" for k, v in grammar.purpose_min_s.items())
            problems.append(
                f"a {duration:g}-second ad gets exactly {max_scenes} shots (code picks the count "
                f"from the length: {mins}, every other shot at least {grammar.kept_min:g} s), "
                f"not {len(draft.scenes)}: merge or split beats, keep the concept"
            )
        problems += [
            f"scene {i} subject_action {problem}"
            for i, s in enumerate(draft.scenes, 1)
            if (problem := multi_action_problem(s.shot.subject_action))
        ]
        problems += [f"scene {i} sets vocab: use null in this format" for i, s in
                     enumerate(draft.scenes, 1) if s.vocab is not None]  # fmt: skip
        if product := _presence(draft):
            problems.append(product)
        if problems:
            raise ValueError("; ".join(problems))
        return Storyboard.from_draft(
            draft, concept_id=concept.id, target_duration_s=duration, fmt=fmt
        )

    def _montage(draft: StoryboardDraft) -> Storyboard:
        """Montage grammar (formats.toml): counts, kept durations, hero last, people, camera, and
        one vocabulary entry per shot. The scene flags are set FROM the entry (ADR-0018), before
        any check, so "spray" in a product-free mist shot never makes it a product shot."""
        draft = flags_from_vocab(draft, vocab)
        problems = grammar_problems(draft.scenes, grammar, vocab.shots)
        problems += hidden_person_problems(draft, vocab)
        problems += [
            f"scene {i} subject_action {problem}"
            for i, s in enumerate(draft.scenes, 1)
            if (problem := multi_action_problem(s.shot.subject_action))
        ]
        if product := _presence(draft):
            problems.append(product)
        if problems:
            raise ValueError("; ".join(problems))
        return Storyboard.from_draft(
            draft, concept_id=concept.id, target_duration_s=duration, fmt=fmt
        )

    prompt = ctx.prompts.get("storyboard").render(
        brief=brief_block(state.brief),
        concept=concept_block(concept),
        duration=f"{duration:g}",
        min_scene=f"{min_s:g}",
        max_scene=f"{max_s:g}",
        single_max=f"{single_max:g}",
        video_profile=profile_block_for(ctx.settings, state),
        max_scenes=str(max_scenes),
        format=fmt.value,
        format_block=format_block(ctx.settings.formats, fmt),
        vocabulary=vocab.prompt_block() if montage else "(not used in this format)",
        aspect_ratio=state.brief.aspect_ratio,
        user_language=language_name(state.brief.language),
    )
    result = await ctx.llm.call_and_convert(LLMNode.STORYBOARD, prompt, StoryboardDraft, convert)
    sb = result.value
    overrides = enforce_scene_flags(sb.scenes)  # generic EN/AR terms; bible terms after continuity
    _, flag_events = flag_override_events(ctx.run_id, Stage.STORYBOARD, overrides)
    summary = " | ".join(
        f"{s.order}:{s.vocab or s.purpose.value} {s.shot.shot_type.value}/"
        f"{s.shot.camera_movement.value} {s.duration_s:g}s"
        for s in sb.scenes
    )
    return NodeResult(
        deltas=[SetStoryboard(storyboard=sb)],
        events=[
            *flag_events,
            decision(ctx.run_id, Stage.STORYBOARD, f"Storyboard ({sb.format.value}): {summary}"),
        ],
        llm_calls=result.calls,
    )


def flags_from_vocab(draft: StoryboardDraft, vocab: VocabLibrary) -> StoryboardDraft:
    """A copy whose scene flags are set from each scene's vocabulary entry (ADR-0018)."""
    draft = draft.model_copy(deep=True)
    for s in draft.scenes:
        if (entry := vocab.get(s.vocab)) is not None:
            s.shows_character, s.shows_product = entry.shows_person, entry.needs_product
    return draft


def hidden_person_problems(draft: StoryboardDraft, vocab: VocabLibrary) -> list[str]:
    """A person written into a person-free entry would be a full figure by stealth: repair it."""
    problems = []
    for i, s in enumerate(draft.scenes, 1):
        entry = vocab.get(s.vocab)
        if entry is not None and not entry.shows_person:
            who = person_mentions(s.description, s.shot.subject_action, s.shot.composition)
            if who:
                problems.append(
                    f"scene {i} vocab {s.vocab!r} shows no person, but the text mentions "
                    f"{', '.join(who[:3])}: remove the person, or use hand_cuff, "
                    "jawline_chest or back_silhouette for the one person shot"
                )
    return problems
