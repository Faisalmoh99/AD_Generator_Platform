"""Cultural wardrobe lexicon (PLAN_V2 item 4, ADR-0019): data in ``config/culture.toml``.

The lexicon is text for three prompts (continuity, prompt writer, keyframe gate) plus a
deterministic lint on the continuity bible's character, so a bible that describes a kurta instead
of a Saudi thobe is repaired before any image is made.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel
from adsmith.domain.brief import Brief
from adsmith.domain.continuity import CharacterSpec
from adsmith.domain.enums import Language

# "concealed placket with no visible buttons" is the rule itself, not a violation.
_NEGATED_CLAUSE = re.compile(r"\b(no|not|never|without|free of|instead of)\b[^.;:,]*", re.I)


class WardrobeLint(DomainModel):
    pattern: str = Field(min_length=1)
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        re.compile(self.pattern)
        return self


class WardrobeEntry(DomainModel):
    label: str
    applies_when: list[str] = Field(default_factory=list)
    describe: list[str] = Field(min_length=1)
    avoid: list[str] = Field(default_factory=list)
    lint: list[WardrobeLint] = Field(default_factory=list)


class CultureLexicon(DomainModel):
    wardrobe: dict[str, WardrobeEntry] = Field(default_factory=dict)

    def applicable(self, brief: Brief | None) -> list[WardrobeEntry]:
        if brief is None:
            return []
        text = " ".join([brief.raw_request, brief.target_audience, *brief.cultural_notes]).lower()
        return [
            e
            for e in self.wardrobe.values()
            if brief.language is Language.AR or any(t in text for t in e.applies_when)
        ]

    def prompt_block(self, brief: Brief | None) -> str:
        entries = self.applicable(brief)
        if not entries:
            return "(none)"
        lines: list[str] = []
        for e in entries:
            lines.append(f"{e.label}: describe it as")
            lines += [f"- {d}" for d in e.describe]
            if e.avoid:
                lines.append("Never confuse it with:")
                lines += [f"- {a}" for a in e.avoid]
        return "\n".join(lines)

    def wardrobe_problems(self, brief: Brief | None, character: CharacterSpec | None) -> list[str]:
        """Lint the bible's character against the applicable lexicon entries (→ schema repair)."""
        if character is None:
            return []
        fields = {
            "character.description": character.description,
            "character.appearance": character.appearance,
            **{f"character.wardrobe[{i}]": w for i, w in enumerate(character.wardrobe)},
        }
        problems: list[str] = []
        for entry in self.applicable(brief):
            for rule in entry.lint:
                for name, text in fields.items():
                    m = re.search(rule.pattern, _NEGATED_CLAUSE.sub(" ", text), re.IGNORECASE)
                    if m:
                        problems.append(f"{name} says {m.group(0)!r}: {rule.message}")
        return problems
