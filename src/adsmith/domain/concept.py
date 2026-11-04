"""Ad concepts and the 3-concept set the user (or judge) picks from."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel, new_id
from adsmith.domain.similarity import DISTINCTNESS_THRESHOLD, join_fields, too_similar_pairs

UserText = Annotated[str, Field(min_length=1, max_length=240)]
CONCEPT_COUNT = 3


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
    estimated_scene_count: int = Field(ge=2, le=6)

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


class Concept(ConceptDraft):
    id: str = Field(default_factory=lambda: new_id("cpt"))


class ConceptSet(DomainModel):
    concepts: list[Concept] = Field(min_length=CONCEPT_COUNT, max_length=CONCEPT_COUNT)
    category_cliches: list[Cliche] = Field(default_factory=list, max_length=5)
    selected_id: str | None = None
    selected_by: Literal["user", "agent"] | None = None
    selection_rationale: str | None = Field(default=None, max_length=1000)

    @classmethod
    def from_draft(cls, draft: ConceptSetDraft) -> ConceptSet:
        return cls(
            concepts=[Concept(**c.model_dump()) for c in draft.concepts],
            category_cliches=list(draft.category_cliches),
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
JUDGE_WEIGHTS: dict[str, float] = {
    "brief_fit": 0.20,
    "originality": 0.20,  # fresh idea vs. the category clichés listed at ideation
    "insight": 0.15,  # a real human truth, not just a striking visual (M4c)
    "producibility": 0.20,  # can current video models render it well in ≤15 s?
    "product_hero": 0.15,
    "cultural_fit": 0.10,
}
# Fixed deductions when the judge itself flags a problem, so a flagged concept can't win on its
# other strengths alone (ADR-0014).
CLICHE_PENALTY = 1.5
IMPLAUSIBLE_SHOT_PENALTY = 1.5
Score = Annotated[int, Field(ge=1, le=10)]


class ConceptScore(DomainModel):
    concept_number: int = Field(ge=1, le=CONCEPT_COUNT, description="1, 2 or 3, in given order")
    brief_fit: Score
    producibility: Score
    product_hero: Score
    cultural_fit: Score
    originality: Score
    insight: Score
    single_shot_plausible: bool = Field(
        description="True only if every beat can be filmed as one continuous shot per scene. "
        "Scale tricks, transformations and reveals that need an edit make this false."
    )
    uses_category_cliche: bool = Field(
        description="True if the concept's core idea or main visual relies on a category cliché "
        "(listed or otherwise)."
    )
    rationale: Annotated[str, Field(min_length=1, max_length=400)]

    def weighted(self) -> float:
        total: float = sum(float(getattr(self, k)) * w for k, w in JUDGE_WEIGHTS.items())
        if self.uses_category_cliche:
            total -= CLICHE_PENALTY
        if not self.single_shot_plausible:
            total -= IMPLAUSIBLE_SHOT_PENALTY
        return round(total, 4)


class ConceptJudgementDraft(DomainModel):
    """``select`` output. The LLM scores; code computes the weighted total and the winner."""

    scores: list[ConceptScore] = Field(min_length=CONCEPT_COUNT, max_length=CONCEPT_COUNT)

    @model_validator(mode="after")
    def _one_score_per_concept(self) -> Self:
        numbers = sorted(s.concept_number for s in self.scores)
        if numbers != list(range(1, CONCEPT_COUNT + 1)):
            raise ValueError(f"need exactly one score for each concept 1..3, got {numbers}")
        return self

    def winner(self) -> ConceptScore:
        """Highest weighted score; ties: brief fit, then originality, then lowest number."""
        return max(
            self.scores,
            key=lambda s: (s.weighted(), s.brief_fit, s.originality, -s.concept_number),
        )
