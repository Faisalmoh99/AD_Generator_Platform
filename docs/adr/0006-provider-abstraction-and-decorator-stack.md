# ADR-0006: Provider abstraction and decorator stack

- Status: Accepted (2026-11-04)

## Context
We need Anthropic (default) and OpenAI for LLMs, mock and fal.ai for images and video, and
cross-cutting behaviour (caching, chaos fault injection, cost metering) that must not leak into
provider code or the agent. Video models also differ in prompt style and parameter names
(`image_url` vs `start_image_url`, allowed durations, negative-prompt support).

## Decision
- Narrow `Protocol` interfaces: `LLMProvider.structured(...)`, `ImageProvider.generate_image(...)`,
  `VideoProvider.image_to_video(...)` with declared `VideoCapabilities`.
- A **prompt adapter layer** (`KlingAdapter`, `SeedanceAdapter`, `VeoAdapter`, `MockAdapter`) maps
  the provider-agnostic `ScenePrompt` + duration onto each model's payload (param names, duration
  snapping, negative-prompt folding, length limits). Golden-payload tests cover it.
- Cross-cutting concerns are **decorators composed at wiring time**:
  `CachingProvider(ChaosWrapper(BudgetMeter(FalProvider | MockProvider)))`.
  The cache is outermost: a hit costs nothing, cannot be faulted by chaos, and is traced as
  `cache_hit`.
- Endpoints, model IDs and prices come only from `config/models.toml`.

## Alternatives considered
- Inheritance (`CachedFalProvider`, …): combinatorial explosion.
- A multi-provider gateway library (LiteLLM etc.): convenient for chat, weak for structured outputs,
  vision and media generation semantics; one more abstraction to debug.

## Consequences
- + Swapping providers or models is a config change. Chaos and cache are testable on their own.
- + Mock and real providers go through identical agent code paths.
- − Each new video model needs an adapter (small and well-tested).
