"""Map provider SDK exceptions onto the failure taxonomy. The ONE place to update when a provider
changes its error format. Ordered most-specific first, because SDK exception classes inherit from
one another (e.g. ``APITimeoutError`` is an ``APIConnectionError``)."""

from __future__ import annotations

import anthropic
import openai

from adsmith.agent.recovery.errors import (
    AccountLockedError,
    AgentError,
    ContentPolicyError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)

_SERVER_ERROR = 500
_REQUEST_TIMEOUT = 408
_CONFLICT = 409
_TOO_MANY_REQUESTS = 429


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


# fal returns content-safety rejections as 4xx with a typed detail (e.g.
# {"detail": [{"type": "content_policy_violation", "msg": ...}]}); match the type/message.
_FAL_POLICY_MARKERS = ("content_policy", "policy_violation", "nsfw", "safety", "flagged")
# Account-level refusal (seen live in M11: HTTP 403 "User is locked. Reason: Exhausted balance").
_FAL_BALANCE_MARKERS = ("exhausted balance", "top up your balance")


def classify_fal_http(
    status: int, body: str, headers: object = None, *, endpoint: str = ""
) -> AgentError:
    """Map a fal HTTP error (submit, status or result) onto the taxonomy."""
    snippet = body[:600]
    msg = f"fal {endpoint}: HTTP {status}: {snippet}"
    lowered = body.lower()
    if status == _TOO_MANY_REQUESTS:
        return RateLimitedError(msg, retry_after_s=_retry_after(headers))
    if status >= _SERVER_ERROR or status in {_REQUEST_TIMEOUT, _CONFLICT}:
        return TransientProviderError(msg)
    if any(m in lowered for m in _FAL_BALANCE_MARKERS):
        return AccountLockedError(f"{msg} (the fal account has no usable balance)")
    if any(m in lowered for m in _FAL_POLICY_MARKERS):
        return ContentPolicyError(msg, category="fal_safety")
    if status in {401, 403}:
        return PermanentProviderError(f"{msg} (check FAL_KEY)")
    return PermanentProviderError(msg)


# Higgsfield (docs.higgsfield.ai, read 2026-11-06). Errors are JSON with ``code`` and sometimes
# ``retryable``: a full account queue is 429 ``account_queue_full`` with Retry-After; admission
# control is 503 ``admission_control_unavailable``. The exact body of a credit refusal on a
# generation endpoint is NOT documented (the agent API uses 402): matched by markers and verified
# at the first real call.
_HF_PAYMENT_REQUIRED = 402
_HF_CREDIT_MARKERS = ("insufficient", "credits", "balance", "top up")
_HF_POLICY_MARKERS = ("nsfw", "content_policy", "policy", "safety", "moderation", "flagged")


def classify_higgsfield_http(  # noqa: PLR0911 - flat mapping
    status: int, body: str, headers: object = None, *, endpoint: str = ""
) -> AgentError:
    """Map a Higgsfield HTTP error (upload, estimate, submit or status) onto the taxonomy."""
    snippet = body[:600]
    msg = f"higgsfield {endpoint}: HTTP {status}: {snippet}"
    lowered = body.lower()
    if status == _TOO_MANY_REQUESTS:
        return RateLimitedError(msg, retry_after_s=_retry_after(headers))
    if status >= _SERVER_ERROR or status in {_REQUEST_TIMEOUT, _CONFLICT}:
        return TransientProviderError(msg)
    if status == _HF_PAYMENT_REQUIRED or any(m in lowered for m in _HF_CREDIT_MARKERS):
        return AccountLockedError(f"{msg} (the Higgsfield account has no usable balance)")
    if any(m in lowered for m in _HF_POLICY_MARKERS):
        return ContentPolicyError(msg, category="higgsfield_safety")
    if '"retryable": true' in lowered or '"retryable":true' in lowered:
        return TransientProviderError(msg)
    if status in {401, 403}:
        return PermanentProviderError(f"{msg} (check HF_KEY)")
    return PermanentProviderError(msg)


# Kie.ai (docs.kie.ai, read 2026-11-07, ADR-0027). The API answers HTTP 200 even on errors: the
# JSON body's ``code`` carries the error (401 key, 402 credits, 408 no result for 10 min, 422
# validation, 429 rate limit, 455 maintenance, 500 server, 501 generation failed, 505 feature
# disabled). A task that ends in state ``fail`` carries ``failCode``/``failMsg`` and is not charged.
_KIE_TRANSIENT = frozenset({408, 455, 500, 502, 503, 504})
_KIE_POLICY_MARKERS = ("nsfw", "sensitive", "policy", "moderation", "safety", "flagged", "violat")
_KIE_CREDIT_MARKERS = ("insufficient", "credits", "balance")


def classify_kie(code: int, msg: str, headers: object = None, *, step: str = "") -> AgentError:
    """Map a Kie body ``code`` (or an HTTP status, same numbers) onto the taxonomy."""
    text = f"kie {step}: code {code}: {msg[:600]}"
    lowered = msg.lower()
    if code == _TOO_MANY_REQUESTS:
        return RateLimitedError(text, retry_after_s=_retry_after(headers))
    if code in _KIE_TRANSIENT or code > _SERVER_ERROR + 5:
        return TransientProviderError(text)
    if code == 402 or (code != 401 and any(m in lowered for m in _KIE_CREDIT_MARKERS)):
        return AccountLockedError(f"{text} (the Kie account has no usable credits)")
    if code == 401:
        return PermanentProviderError(f"{text} (check KIE_API_KEY)")
    if any(m in lowered for m in _KIE_POLICY_MARKERS):
        return ContentPolicyError(text, category="kie_safety")
    return PermanentProviderError(text)
