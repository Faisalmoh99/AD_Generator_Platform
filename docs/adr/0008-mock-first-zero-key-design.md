# ADR-0008: Mock-first, zero-key design

- Status: Accepted (2026-11-04)

## Context
Reviewers must be able to run the project in under two minutes, and the test suite must be
deterministic and offline. Real video generation is slow and paid, and real LLMs are
nondeterministic.

## Decision
- **Media:** `MockImageProvider` (Pillow cards showing scene number, shot spec and palette from the
  bible) and `MockVideoProvider` (ffmpeg motion matching `camera_movement` over the keyframe, exact
  duration, overlay with scene/version/attempt). Both have configurable latency and failure
  injection. This is the default.
- **LLM:** `ScriptedLLMProvider` returns validated canned outputs per node (EN + AR perfume-ad
  fixtures) and can inject invalid JSON and refusals. It powers the unit tests and **Quickstart A**
  (`ADSMITH_LLM_PROVIDER=scripted`, zero keys). **Quickstart B** uses a real Anthropic key (+
  optional `FAL_KEY`).
- **QA:** `qa_mode=auto` uses a `MockCritic` with mock media (a real vision critic would always fail
  placeholder frames) and the LLM critic with real media. Chaos Mode drives mock QA rejections.
- Mock and real providers share the agent code path exactly. Only wiring differs.

## Alternatives considered
- Requiring keys for everything: blocks quick evaluation; flaky tests.
- VCR-style recorded HTTP cassettes: brittle against SDK changes, and cassettes risk capturing secrets.

## Consequences
- + `uv sync && uv run adsmith run "..." --auto` works offline and produces a real mp4.
- + Every recovery path is reproducible with seeded fault injection.
- − Scripted LLM output does not respond to arbitrary requests. Quickstart A is a faithful *flow*
  demo, not a creativity demo. This is stated clearly in the README.
