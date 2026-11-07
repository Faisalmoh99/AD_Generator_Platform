"""copy: the end card's brand name and tagline, written by the system (ADR-0028 part 2).

Two LLM calls, code decides: the ``copywriter`` writes five taglines tied to the chosen
concept's insight (and its culture anchor's saying, when it has one) and three fictional brand
names when the brief has none; code checks every line (length, script, empty luxury words,
claims, religious words, real brands → repair); the ``copy_judge`` scores each line on
``config/copy_rubric.toml``; code weights the scores and picks. The pick goes on the end card and
is shown at the spend gate with the other four. A brand or tagline the client gave (the brief, or
``--endcard-brand`` / ``--endcard-tagline``) always wins; with both given the stage costs nothing.
"""

from __future__ import annotations

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetCopySelection, SetEndCard
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import brief_block, concept_block, decision, language_name
from adsmith.config import LLMNode
from adsmith.domain.base import stable_hash
from adsmith.domain.copy import (
    CopyJudgementDraft,
    CopySetDraft,
    copy_dialect,
    copy_set_problems,
    pick,
)
from adsmith.domain.endcard import EndCardCopy
from adsmith.domain.enums import Language, Stage
from adsmith.domain.run import RunState


def _anchor_block(state: RunState, ctx: NodeContext) -> str:
    """The chosen concept's insight and the saying or ritual it stands on (culture bank)."""
    concept = state.concepts.selected if state.concepts else None
    found = state.insights.insights if state.insights else []
    if concept is None or not concept.insight_number or concept.insight_number > len(found):
        return "(no insight recorded: use the concept's own insight)"
    x = found[concept.insight_number - 1]
    lines = [f"Insight: {x.statement}", f"Tension: {x.tension}"]
    entry = ctx.settings.culture_bank.get(x.culture_anchor)
    if entry is not None:
        lines.append(f"Culture anchor: «{entry.text_ar}» ({entry.kind}): {entry.meaning_en}")
        if entry.pitfalls:
            lines.append(f"Never: {'; '.join(entry.pitfalls)}")
    return "\n".join(lines)


def _sayings_block(state: RunState, ctx: NodeContext) -> str:
    """Proverbs and phrases of the culture bank that fit the brief: material to echo."""
    assert state.brief is not None
    bank = ctx.settings.culture_bank
    sayings = [e for e in bank.for_brief(state.brief, limit=40) if e.kind in {"proverb", "phrase"}]
    if not sayings:
        return "(none)"
    return "\n".join(f"- «{e.text_ar}»: {e.meaning_en}" for e in sayings)


async def copywriter(state: RunState, ctx: NodeContext) -> NodeResult:
    b = state.brief
    assert b is not None and state.concepts is not None
    concept = state.concepts.selected
    assert concept is not None
    lang = b.tagline_language or b.language
    if b.brand_name and b.tagline:
        card = EndCardCopy(brand_name=b.brand_name, tagline=b.tagline, language=lang)
        note = "Copy: the client gave the brand and the tagline; nothing to write"
        return NodeResult(
            deltas=[SetEndCard(endcard=card)], events=[decision(ctx.run_id, Stage.COPY, note)]
        )
    arabic = lang is Language.AR
    dialect = copy_dialect(b.raw_request, arabic=arabic)
    need_brands = not b.brand_name
    h = stable_hash(
        "copy.v1",
        {"concept": concept.id, "brand": b.brand_name, "tagline": b.tagline, "dialect": dialect},
    )
    if state.copy_selection is not None and state.copy_selection.prompt_hash == h:
        return NodeResult(
            events=[decision(ctx.run_id, Stage.COPY, "Copy: already written for this concept")]
        )
    rubric = ctx.settings.copy_rubric
    common = {
        "brief": brief_block(b),
        "concept": concept_block(concept),
        "anchor": _anchor_block(state, ctx),
        "dialect": dialect,
        "language": language_name(lang),
        "rubric": rubric.prompt_block(),
        "banned": rubric.banned_block(),
        "max_words": str(rubric.max_words),
    }
    writer = ctx.prompts.get("copywriter").render(
        **common,
        sayings=_sayings_block(state, ctx),
        brands=(
            "The brief has no brand: write exactly 3 fictional brand names."
            if need_brands
            else f"The brand is «{b.brand_name}»: write no brand names (brands: [])."
        ),
    )

    def check(d: CopySetDraft) -> CopySetDraft:
        problems = copy_set_problems(d, rubric=rubric, arabic=arabic, need_brands=need_brands)
        if problems:
            raise ValueError("; ".join(problems))
        return d

    written = await ctx.llm.call_and_convert(LLMNode.COPYWRITER, writer, CopySetDraft, check)
    draft = written.value
    candidates = "\n".join(
        f"{i}. «{t.text}» ({t.device}; echoes: {t.echoes or '-'}; gloss: {t.gloss_en})"
        for i, t in enumerate(draft.taglines, 1)
    )
    brand_list = "\n".join(f"{i}. «{x.name}»: {x.rationale}" for i, x in enumerate(draft.brands, 1))
    judge = ctx.prompts.get("copy_judge").render(
        **common, taglines=candidates, brands=brand_list or "(none: score no brands, brands: [])"
    )

    def check_scores(j: CopyJudgementDraft) -> CopyJudgementDraft:
        if sorted(x.number for x in j.brands) != list(range(1, len(draft.brands) + 1)):
            raise ValueError(f"score each of the {len(draft.brands)} brand names exactly once")
        return j

    judged = await ctx.llm.call_and_convert(
        LLMNode.COPY_JUDGE, judge, CopyJudgementDraft, check_scores
    )
    sel = pick(draft, judged.value, rubric, dialect=dialect, prompt_hash=h)
    brand = b.brand_name or sel.brand or ""
    tagline = b.tagline or sel.tagline
    card = EndCardCopy(
        brand_name=brand, tagline=tagline, language=lang,
        brand_placeholder=not b.brand_name, tagline_placeholder=False,
    )  # fmt: skip
    ranked = sorted(sel.taglines, key=lambda t: -(t.weighted or 0.0))
    summary = f"Copy ({dialect}): «{brand}» — «{tagline}» picked by code; " + "; ".join(
        f"«{t.text}» {t.weighted:.2f}" for t in ranked
    )
    return NodeResult(
        deltas=[SetCopySelection(selection=sel), SetEndCard(endcard=card)],
        events=[
            decision(
                ctx.run_id, Stage.COPY, summary, dialect=dialect, brand=brand, tagline=tagline,
                taglines=[t.model_dump() for t in sel.taglines],
                brands=[x.model_dump() for x in sel.brands],
            )
        ],
        llm_calls=[*written.calls, *judged.calls],
    )  # fmt: skip
