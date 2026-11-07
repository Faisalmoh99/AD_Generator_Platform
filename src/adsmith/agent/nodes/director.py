"""director: the creative director reviews the whole-ad prompt before the spend gate (ADR-0026).

Round 1 rules on every judged creative rule (by id) and returns a full rewrite. Code decides:
- every rule passed → the writer's prompt stays (the rewrite is only logged);
- a rule failed → round 2 confirms or fixes the round-1 rewrite, and the rewrite replaces the
  writer's prompt (it already passed every code check, or the repair loop would have refused it);
- the director's output never passes the code checks → the writer's prompt stays, with a warning.
The director may re-time shots within the format's grammar; the storyboard is then synced. The
same writer prompt is never reviewed twice (``prompt_hash``). The review never blocks the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from adsmith.agent.context import NodeContext, ad_endpoint
from adsmith.agent.deltas import Delta, SetAdGeneration, SetDirectorReview, SyncStoryboardDurations
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import (
    brief_block,
    concept_block,
    decision,
    language_name,
    storyboard_block,
)
from adsmith.agent.nodes.photo import product_photo_parts
from adsmith.agent.nodes.prompt import cliches_block, prop_image_names
from adsmith.agent.prompts import RenderedPrompt
from adsmith.agent.recovery.errors import InvalidLLMOutputError
from adsmith.config import LLMNode
from adsmith.domain.ad import (
    AdGeneration,
    AdPrompt,
    AdPromptDraft,
    budgeted_length,
    length_problem,
    prompt_diff_summary,
    render_ad_prompt,
    timecode_problems,
)
from adsmith.domain.base import stable_hash
from adsmith.domain.creative_rules import CreativeRules, creative_rule_problems
from adsmith.domain.culture_bank import with_gulf_objects
from adsmith.domain.director import (
    DirectorReview,
    DirectorReviewDraft,
    DirectorRound,
    Outcome,
)
from adsmith.domain.enums import Language, Stage
from adsmith.domain.events import AgentEvent
from adsmith.domain.formats import FormatGrammar, format_block
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.run import RunState
from adsmith.domain.storyboard import Storyboard

RETIME_EPS_S = 0.05


def prompt_hash(d: AdPromptDraft) -> str:
    """The prompt's CONTENT, not its rendered text: the PROPS stage renumbers the References
    line afterwards, which must not count as a new prompt."""
    return stable_hash("director.reviewed.v2", d.model_dump(mode="json"))


def durations_of(d: AdPromptDraft) -> list[float]:
    return [round(s.end_s - s.start_s, 3) for s in d.shots]


def rewrite_problems(
    d: AdPromptDraft,
    *,
    sb: Storyboard,
    grammar: FormatGrammar,
    rules: CreativeRules,
    duration_s: int,
    max_chars: int,
    image_names: Sequence[str],
    identity: str | None = None,
    glosses: Mapping[str, str] | None = None,
) -> list[str]:
    """Code checks on a director rewrite. Timecodes are checked against the rewrite's OWN
    durations (the director may re-time), within the grammar: same shot count as the storyboard,
    each shot within the format's length range and its purpose's minimum, ending at the ad's
    length, plus every creative rule and the prompt limit."""
    problems = timecode_problems(d, None, duration_s=duration_s)
    if len(d.shots) != len(sb.scenes):
        problems.append(
            f"keep exactly {len(sb.scenes)} shots (one per storyboard scene), not {len(d.shots)}"
        )
    for s, scene in zip(d.shots, sb.scenes, strict=False):
        length = s.end_s - s.start_s
        if not grammar.kept_min - 1e-9 <= length <= grammar.kept_max + 1e-9:
            problems.append(
                f"shot {s.number} lasts {length:g} s; this format allows "
                f"{grammar.kept_min:g}-{grammar.kept_max:g} s"
            )
        need = grammar.purpose_min_s.get(scene.purpose.value)
        if need is not None and length + 1e-9 < need:
            problems.append(
                f"shot {s.number} is the {scene.purpose.value} shot: at least {need:g} s"
            )
    if not 1 <= d.key_shot <= len(d.shots):
        problems.append(f"key_shot {d.key_shot} is not one of the shots 1..{len(d.shots)}")
    problems += creative_rule_problems(d, rules)
    too_long = length_problem(
        d, duration_s=duration_s, max_chars=max_chars, existing=image_names,
        glosses=glosses, identity=identity,
    )  # fmt: skip
    if too_long:
        problems.append(too_long)
    return problems


def _ar(state: RunState) -> bool:
    return state.brief is not None and state.brief.language is Language.AR


def _round_summary(state: RunState, rnd: DirectorRound, total: int) -> str:
    failed = rnd.failed
    if _ar(state):
        if not failed:
            return f"مراجعة المخرج (الجولة {rnd.round}): تحققت القواعد كلها ({total})"
        return (
            f"مراجعة المخرج (الجولة {rnd.round}): {len(failed)} من {total} قواعد لم تتحقق "
            f"({', '.join(failed)})، فأُعيدت الكتابة"
        )
    if not failed:
        return f"Creative director (round {rnd.round}): all {total} rules pass"
    return (
        f"Creative director (round {rnd.round}): {len(failed)} of {total} rules failed "
        f"({', '.join(failed)}) → rewrite"
    )


def _outcome_summary(state: RunState, review: DirectorReview) -> str:
    diff = prompt_diff_summary(review.before_text, review.after_text)
    retimed = " (re-timed, storyboard synced)" if review.retimed else ""
    if _ar(state):
        label = {
            "kept": "أُبقي نص الكاتب",
            "rewritten": "اعتُمد نص المخرج",
            "fallback": "أُبقي نص الكاتب لأن إعادة الكتابة لم تجتز الفحص",
        }[review.outcome]
        return f"قرار المخرج: {label}{' (أُعيد التوقيت)' if review.retimed else ''}؛ {diff}"
    label = {
        "kept": "writer's prompt kept",
        "rewritten": "director's rewrite used",
        "fallback": "writer's prompt kept (the rewrite failed the code checks)",
    }[review.outcome]
    return f"Creative director: {label}{retimed}; {diff}"


async def director(state: RunState, ctx: NodeContext) -> NodeResult:  # noqa: PLR0915 - one flow
    g = state.generation
    assert g is not None and state.storyboard is not None and state.brief is not None
    assert state.concepts is not None and state.bible is not None
    concept = state.concepts.selected
    assert concept is not None
    before = g.prompt.text
    if g.human_prompt is not None or g.prompt.key_shot is None:
        # A human's prompt is final; a prompt written before v2 has no structure to review.
        return NodeResult(
            events=[
                decision(
                    ctx.run_id,
                    Stage.DIRECTOR,
                    "Creative director: skipped (the prompt was written by a human or before v2)",
                )
            ]
        )
    h = prompt_hash(g.prompt.to_draft())
    r0 = state.director_review
    if r0 is not None and h in {r0.prompt_hash, r0.result_hash}:
        return NodeResult(
            events=[
                decision(
                    ctx.run_id,
                    Stage.DIRECTOR,
                    "Creative director: this prompt was already reviewed; nothing to do",
                )
            ]
        )
    sb, rules = state.storyboard, ctx.settings.creative_rules
    grammar = ctx.settings.formats.grammar(sb.format)
    ep = ad_endpoint(ctx.settings, state.config.ad_model, state.config.ad_provider)
    names = prop_image_names(state)
    duration = g.prompt.duration_s
    writer = g.prompt.to_draft()
    template = ctx.prompts.get("director")
    images = await product_photo_parts(state, ctx)
    ids = rules.judge_ids
    bank = ctx.settings.culture_bank
    ident = g.prompt.identity or None  # the writer's clause by product type

    def convert_for(round_no: int) -> Callable[[DirectorReviewDraft], DirectorRound]:
        def convert(d: DirectorReviewDraft) -> DirectorRound:
            d = d.model_copy(update={"rewrite": with_gulf_objects(d.rewrite, bank)})
            got = [v.rule_id for v in d.verdicts]
            problems = []
            if sorted(got) != sorted(ids):
                problems.append(f"give exactly one verdict per rule id {ids}; got {got}")
            problems += rewrite_problems(
                d.rewrite, sb=sb, grammar=grammar, rules=rules, duration_s=duration,
                max_chars=ep.max_prompt_chars, image_names=names, identity=ident,
                glosses=bank.glosses([r.name for r in d.rewrite.references]),
            )  # fmt: skip
            if problems:
                raise ValueError("; ".join(problems))
            text = render_ad_prompt(
                d.rewrite, duration_s=duration, aspect_ratio=g.aspect_ratio, image_names=names,
                identity=ident,
            )  # fmt: skip
            retimed = any(
                abs(a - b) > RETIME_EPS_S
                for a, b in zip(durations_of(d.rewrite), durations_of(writer), strict=True)
            )
            rnd = DirectorRound(
                round=round_no, verdicts=d.verdicts, changes=list(d.changes), summary=d.summary,
                rewrite_text=text, retimed=retimed,
            )  # fmt: skip
            drafts[round_no] = d.rewrite
            return rnd

        return convert

    drafts: dict[int, AdPromptDraft] = {}

    def budget_note(current: AdPromptDraft) -> str:
        """Exact, for this draft: the References line it will be sent with, and what is left."""
        _, refs_len, planned = budgeted_length(
            current, duration_s=duration, existing=names,
            glosses=bank.glosses([r.name for r in current.references]), identity=ident,
        )  # fmt: skip
        props = ", ".join(planned) or "none"
        return (
            f"With the images this prompt will be sent with ({props}), the References line "
            f"takes {refs_len} characters; everything else must fit in "
            f"{ep.max_prompt_chars - refs_len}. The code check counts the References lines of "
            "your rewrite's own props."
        )

    def render(
        round_no: int, current: AdPromptDraft, previous: DirectorRound | None
    ) -> RenderedPrompt:
        return template.render(
            brief=brief_block(state.brief) if state.brief else "",
            concept=concept_block(concept),
            bible=state.bible.prompt_block() if state.bible else "",
            format=format_block(ctx.settings.formats, sb.format),
            params=rules.params_block(),
            banned=rules.banned_block(),
            judge=rules.judge_block(),
            lessons=rules.lessons_block(),
            gulf_objects=bank.objects_block(),
            cliches=cliches_block(state),
            storyboard=storyboard_block(sb),
            round=str(round_no),
            previous=(
                "\n".join(
                    f"- {v.rule_id}: {'pass' if v.passed else 'FAIL'}: {v.note}"
                    for v in previous.verdicts
                )
                if previous
                else "(first round)"
            ),
            prompt_text=render_ad_prompt(
                current,
                duration_s=duration,
                aspect_ratio=g.aspect_ratio,
                image_names=names,
                identity=ident,
            ),
            draft_json=current.model_dump_json(indent=1),
            duration=str(duration),
            max_chars=str(ep.max_prompt_chars),
            references_budget=budget_note(current),
            user_refs=", ".join(names) or "none",
            user_language=language_name(state.brief.language) if state.brief else "English",
        )

    calls: list[LLMCallRecord] = []
    rounds: list[DirectorRound] = []
    events: list[AgentEvent] = []
    note = ""
    final: AdPromptDraft | None = None
    try:
        res1 = await ctx.llm.call_and_convert(
            LLMNode.DIRECTOR, render(1, writer, None), DirectorReviewDraft,
            convert_for(1), images=images,
        )  # fmt: skip
        calls += res1.calls
        rounds.append(res1.value)
    except InvalidLLMOutputError as exc:
        calls += exc.llm_calls
        note = "; ".join(exc.validation_errors)[:1000]
    if rounds and rounds[0].failed:
        final = drafts[1]
        try:
            res2 = await ctx.llm.call_and_convert(
                LLMNode.DIRECTOR, render(2, drafts[1], rounds[0]), DirectorReviewDraft,
                convert_for(2), images=images,
            )  # fmt: skip
            calls += res2.calls
            rounds.append(res2.value)
            final = drafts[2]
        except InvalidLLMOutputError as exc:
            calls += exc.llm_calls
            note = (
                "round 2 failed the code checks; the round-1 rewrite is used: "
                + ("; ".join(exc.validation_errors)[:800])
            )
    outcome: Outcome = "fallback" if not rounds else ("rewritten" if final is not None else "kept")
    chosen = final if final is not None else writer
    after = render_ad_prompt(
        chosen, duration_s=duration, aspect_ratio=g.aspect_ratio, image_names=names,
        identity=ident,
    )  # fmt: skip
    retimed = final is not None and any(
        abs(a - b) > RETIME_EPS_S
        for a, b in zip(durations_of(final), durations_of(writer), strict=True)
    )
    review = DirectorReview(
        prompt_hash=h, result_hash=prompt_hash(chosen), rounds=rounds, outcome=outcome,
        before_text=before,
        after_text=after if final is not None else before, retimed=retimed, note=note,
    )  # fmt: skip
    for rnd in rounds:
        events.append(
            decision(
                ctx.run_id, Stage.DIRECTOR, _round_summary(state, rnd, len(ids)),
                round=rnd.round, verdicts=[v.model_dump() for v in rnd.verdicts],
                changes=rnd.changes, director_summary=rnd.summary, rewrite=rnd.rewrite_text,
            )
        )  # fmt: skip
    deltas: list[Delta] = [SetDirectorReview(review=review)]
    if final is not None:
        new_prompt = AdPrompt.from_draft(
            final, duration_s=duration, aspect_ratio=g.aspect_ratio, text=after,
            template_version=template.template_id, bible_version=g.prompt.bible_version,
            identity=ident or "",
        )  # fmt: skip
        new_gen: AdGeneration = g.model_copy(deep=True, update={"prompt": new_prompt})
        deltas.append(SetAdGeneration(generation=new_gen))
        if retimed:
            deltas.append(SyncStoryboardDurations(durations=durations_of(final)))
    level = "warn" if outcome == "fallback" or note else "info"
    outcome_event = decision(
        ctx.run_id, Stage.DIRECTOR, _outcome_summary(state, review),
        outcome=outcome, before=before, after=review.after_text, note=note, retimed=retimed,
    )  # fmt: skip
    events.append(outcome_event.model_copy(update={"level": level}))
    return NodeResult(deltas=deltas, events=events, llm_calls=calls)
