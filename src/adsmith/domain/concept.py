"""Ad concepts and the 3-concept set the user (or judge) picks from."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel, new_id
from adsmith.domain.rubric import RUBRIC_KEYS, Rubric
from adsmith.domain.similarity import (
    DISTINCTNESS_THRESHOLD,
    join_fields,
    similarity,
    too_similar_pairs,
)

UserText = Annotated[str, Field(min_length=1, max_length=240)]
CONCEPT_COUNT = 3
MAX_CANDIDATES = 6  # ideate's candidates on a whole-ad run (ADR-0028 part 3)


class ConceptDraft(DomainModel):
    """One concept from the ``ideate`` node. User-facing text is in the user's language."""

    title: Annotated[str, Field(min_length=1, max_length=80)]
    logline: UserText
    narrative_arc: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        min_length=3, max_length=5
    )
    visual_hook: UserText
    insight: UserText = Field(
        description="The human truth behind the idea: what the audience feels or knows that "
        "makes this ad land (not a description of the visuals)."
    )
    emotional_driver: Annotated[str, Field(min_length=1, max_length=120)]
    differentiator: UserText = Field(description="Why this concept differs from the other two.")
    style_references: list[Annotated[str, Field(max_length=80)]] = Field(
        default_factory=list,
        max_length=6,
        description="Cinematographic descriptors only; no real brands, films or people.",
    )
    risk_notes: list[UserText] = Field(default_factory=list, max_length=5)
    estimated_scene_count: int = Field(ge=1, le=12)

    def signature_text(self) -> str:
        """Text used for distinctness. ``differentiator`` and ``risk_notes`` are excluded on
        purpose: a differentiator talks *about* the other concepts and would inflate overlap."""
        return join_fields(
            [self.title, self.logline, self.visual_hook, self.emotional_driver, *self.narrative_arc]
        )


def _check_distinct(concepts: list[ConceptDraft]) -> None:
    hits = too_similar_pairs([c.signature_text() for c in concepts], DISTINCTNESS_THRESHOLD)
    if hits:
        details = "; ".join(
            f"concepts {i + 1} and {j + 1} are {score:.0%} similar" for i, j, score in hits
        )
        raise ValueError(
            f"concepts are not distinct enough ({details}; max allowed "
            f"{DISTINCTNESS_THRESHOLD:.0%}). Make them differ in premise, setting and visual hook, "
            "not just wording."
        )


Cliche = Annotated[str, Field(min_length=1, max_length=160)]


class ConceptSetDraft(DomainModel):
    """``ideate`` output schema. The distinctness check runs here so failures feed schema repair.

    ``category_cliches`` comes FIRST on purpose: structured output is generated in schema order, so
    the model names the category's clichés before it writes concepts that must avoid them.
    """

    category_cliches: list[Cliche] = Field(
        min_length=3,
        max_length=5,
        description="The most overused ideas and visuals in ads for this product category and "
        "market, written in the user's language. None of the concepts may rely on them.",
    )
    concepts: list[ConceptDraft] = Field(min_length=CONCEPT_COUNT, max_length=CONCEPT_COUNT)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        _check_distinct(self.concepts)
        return self


# ------------------------------------------------------------------------------ creative layer
# Phase C (ADR-0024): an insight stage names the truths first; each concept stands on one of them
# and carries a story with a turn, a hook for the first two seconds, the product's role and the
# cultural meaning. The LLM schemas have no optional fields on purpose (Anthropic's structured
# output rejected a schema as "too complex" with extra nullable fields, STOP 1).
LongText = Annotated[str, Field(min_length=1, max_length=400)]


class InsightDraft(DomainModel):
    statement: UserText = Field(
        description="The human truth, in one sentence the audience would recognise as theirs."
    )
    tension: UserText = Field(description="What makes it uncomfortable, funny or moving: the gap.")
    evidence: UserText = Field(
        description="Where it shows in real life for this audience (a habit, a ritual, a moment)."
    )
    gulf_meaning: UserText = Field(
        description="What it means for a Saudi/Gulf viewer specifically, beyond decoration."
    )
    culture_anchor: str = Field(
        max_length=40,
        description="The id of the culture-bank entry this truth stands on, or 'none'.",
    )


class Insight(InsightDraft):
    """A stored insight; runs made before the culture bank have no anchor."""

    culture_anchor: str = Field(default="none", max_length=40)


class InsightSetDraft(DomainModel):
    """``insight`` output. ``category_cliches`` first (schema order = generation order), so the
    templates to avoid are named before the truths are written."""

    category_cliches: list[Cliche] = Field(min_length=3, max_length=5)
    insights: list[InsightDraft] = Field(min_length=3, max_length=5)


class InsightSet(DomainModel):
    """Stored insights (the draft's shape, with defaults for runs made before ADR-0026)."""

    category_cliches: list[Cliche] = Field(min_length=3, max_length=5)
    insights: list[Insight] = Field(min_length=3, max_length=5)

    @classmethod
    def from_draft(cls, d: InsightSetDraft) -> InsightSet:
        return cls.model_validate(d.model_dump())


class Story(DomainModel):
    setup: UserText
    turn: UserText = Field(description="The one moment that changes the meaning of the setup.")
    payoff: UserText = Field(description="Where it lands, on the product.")


class Hook(DomainModel):
    image: UserText = Field(description="What the viewer sees in the first two seconds.")
    sound: Annotated[str, Field(min_length=1, max_length=160)] = Field(
        description="What the viewer hears in the first two seconds."
    )
    question: UserText = Field(description="The question it opens in the viewer's mind.")


ProductRoleKind = Literal["hero", "catalyst", "reward", "witness"]


class ProductRole(DomainModel):
    role: ProductRoleKind = Field(
        description="hero: the story is about it; catalyst: it makes the turn happen; reward: "
        "it is what the story earns; witness: it is present at the moment that matters."
    )
    line: UserText = Field(description="Why the story needs the product, in one sentence.")


class CreativeConceptDraft(DomainModel):
    """One concept from ``ideate`` v6 (user-facing text in the user's language)."""

    insight_number: int = Field(ge=1, le=5, description="Which insight (1-based) it stands on.")
    title: Annotated[str, Field(min_length=1, max_length=80)]
    logline: UserText
    insight: UserText = Field(description="That insight, said in this concept's own terms.")
    device_id: Annotated[str, Field(min_length=1, max_length=40)] = Field(
        description="The visual device (an id from the listed devices) the idea is shown with."
    )
    device_image: UserText = Field(
        description="The one image only video can show, in one sentence: what the viewer sees "
        "when the device happens."
    )
    story: Story
    hook: Hook
    product_role: ProductRole
    cultural_meaning: UserText = Field(
        description="How the culture carries the idea's meaning (a ritual, a value), not props."
    )
    originality_claim: UserText = Field(
        description="Why this is not a category template: what a viewer has not seen before."
    )
    emotional_driver: Annotated[str, Field(min_length=1, max_length=120)]
    differentiator: UserText = Field(description="Why this concept differs from the other two.")
    shot_sketch: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        min_length=3, max_length=6, description="3-6 one-line shots, in order."
    )
    device_shot: int = Field(
        ge=1, le=6, description="The shot_sketch line (1-based) where the device happens."
    )
    style_references: list[Annotated[str, Field(max_length=80)]] = Field(
        default_factory=list, max_length=6
    )
    risk_notes: list[UserText] = Field(default_factory=list, max_length=5)
    estimated_scene_count: int = Field(ge=1, le=12)
    axis: Annotated[str, Field(min_length=1, max_length=30)] = Field(
        description="The creative axis code assigned to this concept (one of the listed axes)."
    )
    motifs: list[Annotated[str, Field(min_length=1, max_length=30)]] = Field(
        min_length=2,
        max_length=4,
        description="2-4 short English nouns the idea stands on: who, where, what object "
        "(e.g. 'host', 'doorway', 'finjan').",
    )

    def to_concept(self) -> Concept:
        return Concept(
            title=self.title,
            logline=self.logline,
            narrative_arc=[self.story.setup, self.story.turn, self.story.payoff],
            visual_hook=self.hook.image,
            insight=self.insight,
            emotional_driver=self.emotional_driver,
            differentiator=self.differentiator,
            style_references=list(self.style_references),
            risk_notes=list(self.risk_notes),
            estimated_scene_count=self.estimated_scene_count,
            insight_number=self.insight_number,
            story=self.story,
            hook=self.hook,
            product_role=self.product_role,
            cultural_meaning=self.cultural_meaning,
            originality_claim=self.originality_claim,
            shot_sketch=list(self.shot_sketch),
            axis=self.axis,
            motifs=list(self.motifs),
            device_id=self.device_id,
            device_image=self.device_image,
            device_shot=self.device_shot,
        )


class CreativeConceptSetDraft(DomainModel):
    """``ideate`` output: three concepts, or six on a whole-ad run (ADR-0028: code keeps the three
    that differ most), distinct (checked here → schema repair)."""

    device_sketches: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=3,
        max_length=10,
        description="Raw one-line ideas written BEFORE the concepts, ignoring every constraint: "
        "a device id and the invisible thing or impossible change it shows for this product.",
    )
    concepts: list[CreativeConceptDraft] = Field(
        min_length=CONCEPT_COUNT, max_length=MAX_CANDIDATES
    )

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        _check_distinct([c.to_concept() for c in self.concepts])
        return self


class Concept(ConceptDraft):
    id: str = Field(default_factory=lambda: new_id("cpt"))
    # The creative layer (ideate v6). None for concepts made before it (they still load).
    insight_number: int | None = Field(default=None, ge=1, le=5)
    story: Story | None = None
    hook: Hook | None = None
    product_role: ProductRole | None = None
    cultural_meaning: str | None = Field(default=None, max_length=400)
    originality_claim: str | None = Field(default=None, max_length=400)
    shot_sketch: list[str] = Field(default_factory=list, max_length=6)
    axis: str | None = Field(default=None, max_length=30)  # ADR-0028; None before it
    motifs: list[str] = Field(default_factory=list, max_length=4)
    # The visual device (ADR-0029); None before it.
    device_id: str | None = Field(default=None, max_length=40)
    device_image: str | None = Field(default=None, max_length=400)
    device_shot: int | None = Field(default=None, ge=1, le=6)


class ConceptSet(DomainModel):
    concepts: list[Concept] = Field(min_length=CONCEPT_COUNT, max_length=CONCEPT_COUNT)
    category_cliches: list[Cliche] = Field(default_factory=list, max_length=5)
    selected_id: str | None = None
    selected_by: Literal["user", "agent"] | None = None
    selection_rationale: str | None = Field(default=None, max_length=1000)
    # The candidates ideate wrote that code did not keep (ADR-0028), in full, for the record.
    candidates_dropped: list[Concept] = Field(default_factory=list, max_length=MAX_CANDIDATES)

    @classmethod
    def from_draft(cls, draft: ConceptSetDraft) -> ConceptSet:
        return cls(
            concepts=[Concept(**c.model_dump()) for c in draft.concepts],
            category_cliches=list(draft.category_cliches),
        )

    @classmethod
    def from_creative(
        cls,
        draft: CreativeConceptSetDraft,
        cliches: list[str],
        keep: Sequence[int] | None = None,
    ) -> ConceptSet:
        """``keep``: indices of the concepts to keep (``pick_distinct``), in order."""
        idx = list(keep) if keep is not None else list(range(len(draft.concepts)))
        rest = [i for i in range(len(draft.concepts)) if i not in idx]
        return cls(
            concepts=[draft.concepts[i].to_concept() for i in idx],
            category_cliches=cliches,
            candidates_dropped=[draft.concepts[i].to_concept() for i in rest],
        )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_distinct(list(self.concepts))
        ids = [c.id for c in self.concepts]
        if len(set(ids)) != len(ids):
            raise ValueError("concept ids must be unique")
        if (self.selected_id is None) != (self.selected_by is None):
            raise ValueError("selected_id and selected_by must be set together")
        if self.selected_id is not None and self.selected_id not in ids:
            raise ValueError(f"selected_id {self.selected_id!r} is not one of the concepts")
        return self

    @property
    def selected(self) -> Concept | None:
        return next((c for c in self.concepts if c.id == self.selected_id), None)

    def select(self, concept_id: str, by: Literal["user", "agent"], rationale: str | None) -> None:
        if concept_id not in {c.id for c in self.concepts}:
            raise ValueError(f"unknown concept {concept_id!r}")
        self.selected_id, self.selected_by, self.selection_rationale = concept_id, by, rationale


# ------------------------------------------------------------------------------ auto-selection
# The judge scores the six rubric criteria (config/rubric.toml, ADR-0024); code weights them and
# subtracts the cliché penalty, then picks the winner. Ease of execution is not a criterion.
Score = Annotated[int, Field(ge=1, le=10)]


class ConceptScore(DomainModel):
    concept_number: int = Field(ge=1, le=CONCEPT_COUNT, description="1, 2 or 3, in given order")
    insight: Score
    hook: Score
    story_turn: Score
    originality: Score
    cultural_meaning: Score
    product_role: Score
    uses_category_cliche: bool = Field(
        description="True if the concept's core idea or main visual relies on a category cliché "
        "(listed or otherwise)."
    )
    rationale: Annotated[str, Field(min_length=1, max_length=400)]

    def rubric_scores(self) -> dict[str, float]:
        return {k: float(getattr(self, k)) for k in RUBRIC_KEYS}

    def weighted(self, rubric: Rubric) -> float:
        return rubric.weighted(self.rubric_scores(), cliche=self.uses_category_cliche)


class ConceptJudgementDraft(DomainModel):
    """``select`` output. The LLM scores; code computes the weighted total and the winner."""

    scores: list[ConceptScore] = Field(min_length=CONCEPT_COUNT, max_length=CONCEPT_COUNT)

    @model_validator(mode="after")
    def _one_score_per_concept(self) -> Self:
        numbers = sorted(s.concept_number for s in self.scores)
        if numbers != list(range(1, CONCEPT_COUNT + 1)):
            raise ValueError(f"need exactly one score for each concept 1..3, got {numbers}")
        return self

    def winner(self, rubric: Rubric, penalties: Mapping[int, float] | None = None) -> ConceptScore:
        """Highest weighted score minus any code penalty (novelty, ADR-0028); ties: insight,
        then hook, then the lowest number."""
        pen = penalties or {}
        return max(
            self.scores,
            key=lambda s: (
                s.weighted(rubric) - pen.get(s.concept_number, 0.0),
                s.insight,
                s.hook,
                -s.concept_number,
            ),
        )


# ------------------------------------------------------------------------------ diversity
# ADR-0028 part 3: four of five planning runs (2026-11-07) won with "a host waiting for a guest at
# the door". Research on LLM ideation: planned spread over semantic directions beats more samples
# of one prompt; interaction that pulls ideas together collapses diversity. So code assigns each
# candidate an axis, ideate writes six, and code keeps the three that differ most, away from the
# motifs of recent winners.
AXES: dict[str, str] = {
    "hospitality": "a guest, a host, a welcome",
    "family": "parents, children, elders, the home",
    "self": "a private minute of one person, for themselves",
    "occasion": "Eid, a wedding, a gift, a milestone",
    "travel": "the road: leaving, coming back, far from home",
    "craft": "how it is made, chosen, tasted or judged by someone who knows",
    "season": "a time of year: winter outings, Ramadan nights, the harvest",
    "no_anchor": "a human truth with no ritual or tradition at all",
}
_FILL_ORDER = ("self", "family", "craft", "travel", "season", "occasion", "no_anchor")
_STOP = frozenset({"the", "a", "an", "of", "and", "with", "his", "her", "their", "on", "in"})


class MotifEntry(DomainModel):
    """One recent winner in the cross-run motif ledger."""

    run_id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=120)
    axis: str = Field(min_length=1, max_length=30)
    motifs: list[str] = Field(min_length=1, max_length=6)
    device: str | None = Field(default=None, max_length=40)  # ADR-0029; None before it


def assign_axes(anchor_themes: Sequence[str], n: int) -> list[str]:
    """``n`` distinct axes: the themes the insights stand on (in order), then the fill order;
    hospitality at most once and only when an insight stands on it."""
    out: list[str] = []
    for t in [*anchor_themes, *_FILL_ORDER]:
        if t in AXES and t not in out:
            out.append(t)
        if len(out) == n:
            break
    return out


def _words(motif: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", motif.lower()) if len(w) > 2 and w not in _STOP}


def motif_overlap(a: Sequence[str], b: Sequence[str]) -> int:
    """How many of ``a``'s motifs share a content word with one of ``b``'s ('front door' and
    'door' match; 'host' and 'hostess' do not, on purpose: whole words only)."""
    bw = [_words(m) for m in b]
    return sum(1 for m in a if any(_words(m) & w for w in bw))


def recent_overlap(motifs: Sequence[str], ledger: Sequence[MotifEntry]) -> int:
    return max((motif_overlap(motifs, e.motifs) for e in ledger), default=0)


REUSED_MOTIFS = 2  # a concept sharing this many motifs with a recent winner counts as reused
RECENT_DEVICES = 3  # a device one of the last three winners used counts as reused (ADR-0029)


def recent_device(device: str | None, ledger: Sequence[MotifEntry]) -> bool:
    return device is not None and device in {e.device for e in ledger[:RECENT_DEVICES]}


def is_reused(motifs: Sequence[str], device: str | None, ledger: Sequence[MotifEntry]) -> bool:
    """A recent winner's people, place and object, or the device one of the last three used."""
    return recent_overlap(motifs, ledger) >= REUSED_MOTIFS or recent_device(device, ledger)


def pick_distinct(
    concepts: Sequence[ConceptDraft], motifs: Sequence[Sequence[str]],
    ledger: Sequence[MotifEntry], k: int = CONCEPT_COUNT,
    devices: Sequence[str | None] | None = None,
) -> list[int]:  # fmt: skip
    """The indices of the ``k`` concepts to keep, in their written order: concepts that reuse a
    recent winner's motifs or device go last; then greedily the concept least similar to those
    already kept (max-min on ``signature_text``), starting from the first fresh one."""
    n = len(concepts)
    if n <= k:
        return list(range(n))
    devs = list(devices) if devices is not None else [None] * n
    reused = [is_reused(m, devs[i], ledger) for i, m in enumerate(motifs)]
    pool = [i for i in range(n) if not reused[i]]
    pool += [i for i in range(n) if reused[i]][: max(0, k - len(pool))]
    texts = [c.signature_text() for c in concepts]
    kept = [pool[0]]
    while len(kept) < k:
        rest = [i for i in pool if i not in kept]
        kept.append(
            max(rest, key=lambda i: (min(1 - similarity(texts[i], texts[j]) for j in kept), -i))
        )
    return sorted(kept)
