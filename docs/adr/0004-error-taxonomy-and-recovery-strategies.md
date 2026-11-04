# ADR-0004: Error taxonomy and recovery strategies

- Status: Accepted (2026-11-04)

## Context
Generative pipelines fail in qualitatively different ways. Retrying a policy rejection verbatim is
pointless. Backing off on a bad-quality clip wastes time. Crashing on malformed LLM JSON throws away
work. One undifferentiated `try/except: retry` cannot handle these well, and it hides cost.

## Decision
Every failure is **classified** into a `FailureKind` by `recovery/classifier.py`, which maps
provider SDK errors and results to typed `AgentError` subclasses. Each kind gets a dedicated,
bounded strategy:

| Kind | Strategy | Default limit |
|---|---|---|
| transient (5xx, timeout, connection) | exponential backoff with **full jitter**, `uniform(0, min(20, 1·2^n))` s | 4 attempts |
| rate_limited (429) | honour `retry-after`, else the backoff above; per-provider gate | 5 attempts |
| content_policy (provider rejection / LLM refusal) | LLM `policy_rewrite`: remove triggers, keep product + shot + intent | 2 rewrites |
| quality_rejected (QA critic) | regenerate with prompt revised from the critic's concrete fixes | 2 regenerations |
| invalid_output (schema) | schema-repair retry with validation errors fed back | 2 repairs |
| budget_exceeded | no call; scene `failed`; others untouched | $10/run default |
| permanent (4xx, auth) | no retry; clear reason | 0 |
| exhausted | scene `failed` with a user-language reason; run → `done_partial` | — |

Cross-cutting rules:
- **Isolation:** each scene is an independent task. Failures are converted into `SceneFailure` at
  the scene boundary and never propagate.
- **SDK retries are disabled**, so every retry is visible, classified, budgeted and traced.
- **Idempotency:** generation is keyed by `sha256(provider, model, phase, prompt, params,
  input_image)`. Successes are cached; failures are never cached.
- **Budget guard:** estimate → reserve → call → commit/release, plus per-scene and per-run attempt
  caps.
- Sleep, clock and RNG are injected, so every path is deterministically testable.

## Alternatives considered
- Library retry decorators (tenacity) everywhere: good for transient errors, but they cannot express
  "rewrite the prompt" or "revise with critic feedback", and they hide attempts from the trace.
- Unbounded/agentic self-healing (the LLM decides what to try): unpredictable cost and loops.

## Consequences
- + Each failure mode has a named, tested, explainable response, which suits a demo (Chaos Mode).
- + Cost is bounded by construction.
- − The taxonomy must be maintained as providers change error formats; the classifier is the one
  place to update.
