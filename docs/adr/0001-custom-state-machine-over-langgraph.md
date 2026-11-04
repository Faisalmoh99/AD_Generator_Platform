# ADR-0001: Custom state machine over LangGraph

- Status: Accepted (2026-11-04)

## Context
The agent runs a fixed pipeline (`understand → ideate → select → storyboard → continuity → prompt →
keyframe → generate → qa → assemble`) with two human gates (concept choice, storyboard approval).
The hard part is not the linear pipeline. It is the **per-scene** work: each scene has its own
lifecycle (`pending → generating → qa_review → approved | failed`, plus `stale`), has to fail
independently, and has to be partially re-entered after an edit at a specific depth (prompt,
keyframe, or clip only). Evaluators weigh how the brain is built and how errors are handled, so
control flow has to be easy to read, test and defend.

## Decision
A lean, custom state machine:
- `TRANSITIONS`: a declared table from `Stage` to a pure guard function; `next_stage(state)` is pure.
- Nodes are `async (state, ctx) -> StateDelta`. They do not choose the next stage.
- A `SceneScheduler` runs each scene's sub-state machine as an independent asyncio task.
- A snapshot is persisted to SQLite after every transition (our own schema), plus an append-only
  event trace.

## Alternatives considered
- **LangGraph.** Pros: checkpointer, `interrupt` for human gates, LangSmith tracing, ecosystem.
  Cons: per-scene fan-out with partial invalidation needs custom state surgery anyway (`Send` fans
  out at the graph level); checkpoints are opaque blobs that are hard to query ("which scenes
  failed and why"); control flow lives partly in framework reducers; heavy and fast-moving
  dependency.
- **Free-form ReAct agent where the LLM chooses tools/steps.** Rejected: nondeterministic control
  flow, hard to test, hard to bound cost, hard to guarantee isolation of failures.
- **Workflow engine (Temporal/Prefect).** Durable and powerful, but adds infrastructure that is
  disproportionate for a prototype.

## Consequences
- + The whole brain is about 300 lines we own; every transition is unit-testable without mocks.
- + Resume-from-any-stage and "inspect any past state" come straight from snapshots.
- − We give up LangSmith tracing and built-in time travel. We cover that with our `AgentEvent`
  trace (streamed to UI/CLI) and versioned snapshots (`resume --from-version`).
- − We maintain the concurrency/single-writer logic ourselves (covered by tests).
