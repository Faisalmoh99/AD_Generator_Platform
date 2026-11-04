# ADR-0013: Run loop, typed deltas and failure handling

- Status: Accepted (2026-11-04)

## Context
ADR-0001 chose an explicit state machine. This ADR records how one step executes, how nodes
communicate state changes, and what happens when a node fails or the run changes under it.

## Decision
**One step** = load the snapshot → run the node for `state.stage` → **one atomic commit**
(ADR-0011) that applies the node's deltas, books its LLM calls in the budget, appends its events,
and sets `stage = next_stage(state, completed)`.

1. **Typed deltas, not closures.** Nodes return `NodeResult(deltas, events, llm_calls)`. Deltas
   are Pydantic models with a `kind` discriminator (`SetBrief`, `SetConcepts`, `SelectConcept`,
   `SetStoryboard`, `SetBible`, `SetScenePrompts`) applied by one `apply_delta` match. They are
   inspectable, testable and carry their own preconditions (e.g. `SetScenePrompts` records the
   scene versions it was written for). Nodes receive a `NodeContext` that **does not include storage**:
   they physically cannot write state.
2. **Control flow is code.** `next_stage` is a pure table lookup. In interactive mode `ideate` goes
   to the concept gate and the `select` node never runs; in auto mode the LLM *scores* concepts on a
   fixed weighted rubric and code takes the argmax (ties: brief fit, then lowest number).
3. **Failures become run state, never crashes.** A typed `AgentError` (or an unexpected exception,
   classified `permanent` with a traceback in `reason_debug`) moves the run to `errored` with a
   `RunError` whose `reason_user` is in the user's language. The LLM calls already spent are still
   booked. A commit whose deltas would violate an invariant is rejected atomically and becomes a
   `permanent` run error.
4. **The run moved while the node was thinking** (a user command, another process): the commit's
   precondition (`stage` unchanged) raises `StaleWriteError`. The result is **discarded on purpose**,
   a warn-level decision event records it, and the run continues from its new state.
5. **Resume** clears `errored` back to the failed stage, releases scene leases left by a crashed
   process, and re-enters the loop. Nodes only read committed state, so re-running a stage is
   always safe. Generation work is additionally protected by the CAS cache (M5).
6. **Human gates** are persisted stages. Commands (`choose_concept`, later `approve_storyboard`)
   are atomic updates that validate the stage and optionally `expected_version`.
7. **Language** of user-facing text: `brief.language`, or the request's script before a brief
   exists (so an Arabic request that fails in `understand` still gets an Arabic error). The
   `understand` node also overrides the LLM's language call when it contradicts the script the
   user actually typed, and logs a decision event.
8. One run loop per run per process (asyncio lock). Across processes, the stage precondition
   prevents a duplicate loop from double-committing, though it can double-spend one stage. That is
   acceptable for a single-user prototype.

## Alternatives considered
- Nodes mutating state directly or via closures: concise, but the changes become opaque in tests
  and traces.
- Letting the LLM pick the concept outright (no rubric): unexplainable, and not reproducible from
  the trace.
- Auto-retrying the whole stage on any failure: retries belong to the recovery layer, which knows
  *what* failed. Stage-level retry is a human or `resume` decision.

## Consequences
- + Every stage boundary is a durable, inspectable snapshot with matching events, cost and timing
  (`stage_completed` carries duration, cost, LLM call count and repairs).
- + Failure, staleness and resume paths are covered by tests with a scripted provider.
- − New node types require a delta type. That is deliberate friction.
