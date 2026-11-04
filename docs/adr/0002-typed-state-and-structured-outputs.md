# ADR-0002: Typed state and structured LLM outputs

- Status: Accepted (2026-11-04)

## Context
Agents that pass free text between steps fail silently. A missing duration or an invented scene
number only shows up three steps later. Edits, blast-radius computation, caching and resume all
need state they can reason about mechanically.

## Decision
- All agent state is Pydantic v2 models (`Brief`, `Concept`, `Storyboard`, `Scene`, `ShotSpec`,
  `ContinuityBible`, `GenerationAttempt`, `QAVerdict`, `ScenePatch`, `AgentEvent`, `RunState`) with
  `extra="forbid"`, bounded fields and **domain invariants as validators** (e.g. scene durations sum
  to the target ±0.25 s; exactly 3 sufficiently distinct concepts).
- Cinematography vocabulary is enums (`ShotType`, `CameraMove`), so prompts and adapters work from
  a closed set.
- Every LLM call goes through `structured_call()`. It uses the provider's native structured output
  (Anthropic `messages.parse` / `output_config.format`; OpenAI structured parse), validates against
  the schema, and on failure runs a **schema-repair retry** that feeds back the exact validation
  errors (max 2). Refusals and truncation are typed errors, not exceptions from parsing.
- Values the LLM is not trusted with are recomputed in code (e.g. `QAVerdict.passed` from score +
  blockers, concept selection argmax from judge scores).

## Alternatives considered
- Free-text + regex parsing: brittle, untestable.
- JSON mode without schema validation: catches syntax, not semantics.
- Tool-use with forced `tool_choice`: rejected by current Claude models (400), and structured
  outputs are the purpose-built mechanism.

## Consequences
- + Validation errors become feedback to the model instead of crashes.
- + State is diffable, hashable (`Scene.content_hash()` drives caching) and resumable.
- − Schema descriptions are part of the prompt and have to be maintained with the templates.
- − Domain invariants enforced at validation time can make the LLM's job harder. The repair loop
  plus clear constraint statements in prompts mitigate this.
