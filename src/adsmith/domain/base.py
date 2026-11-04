"""Shared base model, IDs, clock and canonical hashing for the domain layer.

The domain layer is pure: no I/O, no config, no providers. Everything here is deterministic.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from ulid import ULID


class DomainModel(BaseModel):
    """Base for every domain model.

    - ``extra="forbid"``: unknown fields are errors (LLM output and persisted JSON alike).
    - ``str_strip_whitespace``: whitespace noise never changes hashes or comparisons.
    - ``validate_assignment`` is deliberately **off**: multi-field state changes (e.g. a scene
      moving to ``failed`` together with its ``failure``) happen in explicit transition methods
      that re-check invariants once at the end. Per-field assignment validation would reject
      the valid intermediate step.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        use_enum_values=False,
    )


def new_id(prefix: str) -> str:
    """Sortable, unique, readable IDs: ``run_01JC...``, ``scn_01JC...``."""
    return f"{prefix}_{ULID()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize(value: Any) -> Any:
    """Recursively NFC-normalise strings so visually identical Arabic/Latin text hashes the same."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, NFC strings, UTF-8 preserved."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def stable_hash(namespace: str, value: Any) -> str:
    """SHA-256 of ``namespace`` + canonical JSON.

    The namespace carries a scheme version (e.g. ``"scene.content.v1"``). Changing what goes into a
    hash means bumping the namespace, which invalidates old cache entries deliberately rather than
    by accident.
    """
    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(canonical_json(value).encode("utf-8"))
    return digest.hexdigest()
