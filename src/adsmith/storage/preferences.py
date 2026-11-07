"""Human preferences at the gates (ADR-0029 part 3): every concept, tagline, brand, prompt and ad
decision a human makes, with the options it beat and the scores the system gave them.

Why: an LLM judge for creative work agrees with people about 73 % of the time, and a judge
trained on pairwise human picks reaches 78 % (LitBench, EACL 2026). Fine-tuning needs a thousand
or more such pairs; until then the same rows calibrate the judges and pick few-shot exemplars.
A write never fails a command: the decision itself is already committed with its event.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import Field, JsonValue

from adsmith.domain.base import DomainModel, utcnow
from adsmith.logging import get_logger
from adsmith.storage.db import Database

log = get_logger(__name__)

PreferenceKind = Literal["concept", "tagline", "brand", "prompt", "ad"]


class PreferenceOption(DomainModel):
    text: str = Field(max_length=6000)
    scores: dict[str, float] = Field(default_factory=dict)
    weighted: float | None = None


class PreferenceRecord(DomainModel):
    """One human decision. ``chosen`` is an index into ``options`` (None: everything rejected);
    ``recommended`` is the system's pick; ``written`` is a human's own text that beat every
    option (a free tagline)."""

    run_id: str = Field(min_length=1, max_length=40)
    kind: PreferenceKind
    options: list[PreferenceOption] = Field(default_factory=list, max_length=8)
    chosen: int | None = Field(default=None, ge=0)
    recommended: int | None = Field(default=None, ge=0)
    written: str | None = Field(default=None, max_length=400)
    context: dict[str, JsonValue] = Field(default_factory=dict)

    def pairs(self) -> list[tuple[str, str]]:
        """(preferred, rejected) text pairs, the shape a pairwise judge trains on."""
        if self.written is not None:
            return [(self.written, o.text) for o in self.options]
        if self.chosen is None:
            return []
        win = self.options[self.chosen].text
        return [(win, o.text) for i, o in enumerate(self.options) if i != self.chosen]


class PreferenceLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, rec: PreferenceRecord) -> None:
        try:
            with self._db.write() as conn:
                conn.execute(
                    "INSERT INTO preferences (run_id, created_at, kind, record) "
                    "VALUES (?, ?, ?, ?)",
                    (rec.run_id, utcnow().isoformat(), rec.kind,
                     json.dumps(rec.model_dump(mode="json"), ensure_ascii=False)),
                )  # fmt: skip
        except sqlite3.Error as exc:
            log.warning("preference write failed", run_id=rec.run_id, error=str(exc))

    def all(self, *, run_id: str | None = None) -> list[PreferenceRecord]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT record FROM preferences WHERE (? IS NULL OR run_id = ?) ORDER BY id",
                (run_id, run_id),
            ).fetchall()
        return [PreferenceRecord.model_validate(json.loads(r["record"])) for r in rows]
