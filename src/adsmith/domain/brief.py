"""The structured understanding of a user's request."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel, new_id
from adsmith.domain.enums import Dialect, Language

AspectRatio = Literal["16:9", "9:16", "1:1"]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
MediumText = Annotated[str, Field(min_length=1, max_length=600)]


class BriefDraft(DomainModel):
    """What the ``understand`` LLM node returns. System fields (id, raw request) are added by code.

    Generation-facing fields (product, style, audience) are in **English** so downstream prompts are
    English. ``language`` / ``dialect`` record how the user wrote, so user-facing text can follow.
    """

    language: Language
    dialect: Dialect
    product: ShortText = Field(description="The product being advertised, in English.")
    product_category: ShortText
    brand_name: str | None = Field(default=None, max_length=80)
    target_audience: ShortText
    tone: list[ShortText] = Field(min_length=1, max_length=6)
    visual_style: ShortText
    duration_s: float = Field(default=15.0, ge=5, le=60)
    aspect_ratio: AspectRatio | None = Field(
        default=None, description="Only if the user specified one; otherwise null."
    )
    requested_shots: int | None = Field(
        default=None, ge=1, le=6,
        description="Only if the user explicitly asked for a number of shots/scenes; else null.",
    )  # fmt: skip
    must_include: list[ShortText] = Field(default_factory=list, max_length=10)
    must_avoid: list[ShortText] = Field(default_factory=list, max_length=10)
    tagline: str | None = Field(default=None, max_length=120)
    tagline_language: Language | None = None
    cultural_notes: list[MediumText] = Field(default_factory=list, max_length=8)
    assumptions: list[MediumText] = Field(
        default_factory=list,
        max_length=10,
        description="Everything inferred rather than stated, in the user's language.",
    )
    normalized_request_en: MediumText

    @model_validator(mode="after")
    def _dialect_matches_language(self) -> Self:
        if self.language is Language.EN and self.dialect is not Dialect.NONE:
            raise ValueError("dialect must be 'none' for English requests")
        if self.language is Language.AR and self.dialect is Dialect.NONE:
            raise ValueError("Arabic requests need a dialect (use 'msa' if unsure)")
        if self.tagline is not None and self.tagline_language is None:
            raise ValueError("tagline_language is required when a tagline is given")
        return self


class Brief(BriefDraft):
    id: str = Field(default_factory=lambda: new_id("brf"))
    raw_request: Annotated[str, Field(min_length=1, max_length=4000)]
    aspect_ratio: AspectRatio = "9:16"  # resolved: request value or configured default

    @classmethod
    def from_draft(
        cls, draft: BriefDraft, *, raw_request: str, default_aspect: AspectRatio
    ) -> Brief:
        data = draft.model_dump()
        data["aspect_ratio"] = draft.aspect_ratio or default_aspect
        return cls(**data, raw_request=raw_request)
