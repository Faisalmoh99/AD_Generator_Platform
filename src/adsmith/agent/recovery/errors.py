"""Typed failure taxonomy (ADR-0004). Every failure in the agent is one of these.

Each error carries the ``LLMCallRecord``s spent before it was raised, so a failed call is still
booked in the budget and trace. Failures cost money too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from adsmith.domain.enums import FailureKind

if TYPE_CHECKING:
    from adsmith.domain.llm import LLMCallRecord


class AgentError(Exception):
    kind: ClassVar[FailureKind]
    injected: bool = False  # set by Chaos Mode so the trace is honest about simulated faults

    def __init__(self, message: str, *, llm_calls: list[LLMCallRecord] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.llm_calls: list[LLMCallRecord] = list(llm_calls or [])


class TransientProviderError(AgentError):
    """Timeouts, connection errors, 5xx, overload. Retried with backoff."""

    kind = FailureKind.TRANSIENT


class RateLimitedError(AgentError):
    kind = FailureKind.RATE_LIMITED

    def __init__(
        self, message: str, *, retry_after_s: float | None = None, **kw: list[LLMCallRecord]
    ) -> None:
        super().__init__(message, **kw)
        self.retry_after_s = retry_after_s


class ContentPolicyError(AgentError):
    """Provider rejected the content, or the LLM refused. Recovered by rewriting the prompt."""

    kind = FailureKind.CONTENT_POLICY

    def __init__(
        self, message: str, *, category: str | None = None, **kw: list[LLMCallRecord]
    ) -> None:
        super().__init__(message, **kw)
        self.category = category


class QualityRejectedError(AgentError):
    kind = FailureKind.QUALITY_REJECTED


class InvalidLLMOutputError(AgentError):
    """Output still failed validation after the repair budget was spent."""

    kind = FailureKind.INVALID_OUTPUT

    def __init__(
        self,
        message: str,
        *,
        validation_errors: list[str] | None = None,
        raw: str | None = None,
        **kw: list[LLMCallRecord],
    ) -> None:
        super().__init__(message, **kw)
        self.validation_errors = list(validation_errors or [])
        self.raw = raw


class BudgetExceededError(AgentError):
    kind = FailureKind.BUDGET_EXCEEDED


class AccountLockedError(BudgetExceededError):
    """The provider account itself refuses work (e.g. fal "User is locked: exhausted balance").
    Account-level: every further paid call would fail too, so the provider pauses ALL paid work
    for the rest of the process instead of letting each scene hit the same wall."""


class PermanentProviderError(AgentError):
    """Bad request, auth, not found: retrying cannot help."""

    kind = FailureKind.PERMANENT


RETRYABLE: tuple[type[AgentError], ...] = (TransientProviderError, RateLimitedError)
