"""The creative director's review of the whole-ad prompt (ADR-0026, phase 2).

The director rules on every judged creative rule by id and returns a full rewrite. Code decides
what is kept: the writer's prompt stays when every rule passed; the rewrite replaces it only when a
rule failed and the rewrite passes every code check; otherwise the writer's prompt stays with a
warning. The review never blocks the run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from adsmith.domain.ad import AdPromptDraft
from adsmith.domain.base import DomainModel, utcnow

Note = Annotated[str, Field(min_length=1, max_length=300)]


class RuleVerdict(DomainModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    passed: bool
    note: Note = Field(description="What in the prompt passes or breaks the rule, concretely.")


class DirectorReviewDraft(DomainModel):
    """``director`` output. Notes, changes and summary in the user's language; the rewrite in
    English (generation-facing)."""

    verdicts: list[RuleVerdict] = Field(min_length=1, max_length=20)
    changes: list[Note] = Field(max_length=8)
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    rewrite: AdPromptDraft


class DirectorRound(DomainModel):
    round: int = Field(ge=1, le=2)
    verdicts: list[RuleVerdict]
    changes: list[str] = Field(default_factory=list)
    summary: str = Field(max_length=500)
    rewrite_text: str = ""  # the rewrite as the model would receive it
    retimed: bool = False

    @property
    def failed(self) -> list[str]:
        return [v.rule_id for v in self.verdicts if not v.passed]


Outcome = Literal["kept", "rewritten", "fallback"]


class DirectorReview(DomainModel):
    """Stored on the run. ``prompt_hash`` identifies the writer prompt that was reviewed, so the
    same prompt is never reviewed (and paid for) twice."""

    prompt_hash: str
    rounds: list[DirectorRound] = Field(default_factory=list, max_length=2)
    outcome: Outcome
    before_text: str
    after_text: str
    retimed: bool = False
    note: str = Field(default="", max_length=1000)  # why a fallback happened
    reviewed_at: datetime = Field(default_factory=utcnow)

    @property
    def failed(self) -> list[str]:
        return self.rounds[0].failed if self.rounds else []
