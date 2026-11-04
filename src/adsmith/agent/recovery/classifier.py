"""Map provider SDK exceptions onto the failure taxonomy. The ONE place to update when a provider
changes its error format. Ordered most-specific first, because SDK exception classes inherit from
one another (e.g. ``APITimeoutError`` is an ``APIConnectionError``)."""

from __future__ import annotations

import anthropic
import openai

from adsmith.agent.recovery.errors import (
    AgentError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)

_SERVER_ERROR = 500
_REQUEST_TIMEOUT = 408
_CONFLICT = 409


def _retry_after(headers: object) -> float | None:
    get = getattr(headers, "get", None)
    if get is None:
        return None
    raw = get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def classify_anthropic(exc: Exception) -> AgentError:  # noqa: PLR0911 - flat mapping
    msg = f"anthropic: {type(exc).__name__}: {exc}"
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitedError(msg, retry_after_s=_retry_after(exc.response.headers))
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return TransientProviderError(msg)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return PermanentProviderError(f"{msg} (check ANTHROPIC_API_KEY)")
    if isinstance(exc, anthropic.BadRequestError | anthropic.NotFoundError):
        return PermanentProviderError(msg)
    if isinstance(exc, anthropic.APIStatusError):
        code = exc.status_code
        if code >= _SERVER_ERROR or code in {_REQUEST_TIMEOUT, _CONFLICT}:
            return TransientProviderError(msg)  # incl. 529 overloaded, 503 unavailable
        return PermanentProviderError(msg)
    return PermanentProviderError(msg)


def classify_openai(exc: Exception) -> AgentError:
    msg = f"openai: {type(exc).__name__}: {exc}"
    if isinstance(exc, openai.RateLimitError):
        return RateLimitedError(msg, retry_after_s=_retry_after(exc.response.headers))
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return TransientProviderError(msg)
    if isinstance(exc, openai.AuthenticationError):
        return PermanentProviderError(f"{msg} (check OPENAI_API_KEY)")
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code >= _SERVER_ERROR or code in {_REQUEST_TIMEOUT, _CONFLICT}:
            return TransientProviderError(msg)
        return PermanentProviderError(msg)
    return PermanentProviderError(msg)
