"""Provider-agnostic LLM interface.

Providers do transport only. They send a request constrained to a JSON schema and return the
**raw** text, usage and stop reason. They never validate against Pydantic. Validation and repair
live in ``agent/structured.py`` so that (a) a failed validation still has its raw output for the
repair turn and its usage for cost accounting, and (b) every provider behaves identically.
Providers raise only classified ``AgentError``s (see ``agent/recovery/classifier.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from adsmith.config import Effort
from adsmith.domain.llm import TokenUsage

StopReason = Literal["end_turn", "max_tokens", "refusal", "other"]


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str
    cache: bool = False  # put a prompt-cache breakpoint after this part


@dataclass(frozen=True, slots=True)
class ImagePart:
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data_b64: str


@dataclass(frozen=True, slots=True)
class UserMessage:
    parts: tuple[TextPart | ImagePart, ...]
    role: Literal["user"] = "user"


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """A previous model turn. ``native`` is the provider's own content blocks (including any
    thinking blocks with signatures), replayed **unchanged** so the history stays append-only."""

    text: str
    native: Any = None
    role: Literal["assistant"] = "assistant"


LLMMessage = UserMessage | AssistantMessage


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str
    messages: tuple[LLMMessage, ...]
    json_schema: dict[str, Any]
    schema_name: str
    model: str
    effort: Effort
    max_tokens: int
    template: str = ""  # "name@version": for logs and the scripted provider
    cache_system: bool = True
    allow_server_fallback: bool = False


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str | None
    stop_reason: StopReason
    usage: TokenUsage
    served_model: str
    latency_ms: int
    refusal_category: str | None = None
    native_assistant: Any = None
    raw_stop_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
