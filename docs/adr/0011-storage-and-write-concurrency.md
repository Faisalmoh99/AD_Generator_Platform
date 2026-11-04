# ADR-0011: Storage and write concurrency

- Status: Accepted (2026-11-04)

## Context
State is persisted after every transition so a run can be paused, inspected and resumed from any
stage. Writers are concurrent: several scene tasks (asyncio) finish at arbitrary times, a human can
edit or approve from the UI or CLI while a run is executing, and the CLI and web server may be
separate processes on the same database. The UI streams the event trace over SSE and resumes with
`Last-Event-ID`, so event ordering must be exact.

## Decision
**SQLite in WAL mode, stdlib `sqlite3`, explicit transactions.**

1. **Mutations, not saves.** Nothing calls "save this RunState I'm holding". All writes go through
   `RunRepository.update(run_id, mutate, reason, expected_version=None)`, which inside one
   `BEGIN IMMEDIATE` transaction loads the latest snapshot, applies `mutate`, bumps
   `state_version`, re-validates the whole aggregate (a JSON round-trip), writes a compressed
   snapshot and appends the events `mutate` returned. Any exception rolls back everything.
2. **No lost updates by construction.** The read happens under SQLite's write lock (one writer at a
   time, across connections and processes, with a 5 s busy timeout). Two writers can never both
   apply to version N. The second one reads N+1 and applies its mutation to that.
3. **Stale results are vetoed by the mutation, not by version numbers.** A scene result carries the
   `scene.version` it was produced for. Its mutation checks that version against the *current*
   scene and raises `StaleWriteError` if the scene was edited meanwhile. The result is then
   intentionally dropped (and logged), never misapplied. The generated media is already in the CAS
   and the generation cache, so if the spec ends up the same again, it is reused at zero cost.
4. **Optimistic locking only where a human acted on a view.** UI/CLI commands such as "approve
   storyboard" may pass `expected_version`. A mismatch raises `ConcurrencyConflictError`; the API
   returns 409 and the client re-reads. Automated writers never pass it, because their
   preconditions are checked semantically (point 3).
5. **Event seq** is `MAX(seq)+1` computed inside the same write transaction. With serialised
   writers this gives strictly monotonic, gap-free numbers (a rollback consumes nothing), and
   commit order equals seq order, so a reader never sees `n+1` before `n`. `PRIMARY KEY(run_id,
   seq)` enforces it. SSE resume is `tail(after_seq=Last-Event-ID)`.
6. **Snapshots are full `RunState` JSON, zlib-compressed**, one row per version, append-only.
   Measured: a fresh 4-scene run is ~16 KB raw / ~3 KB compressed, a typical run (2 attempts, 1 edit
   per scene) ~28 KB / ~3 KB, and a heavy one (6 attempts, 3 edits per scene) ~62 KB / ~4 KB. A run
   produces roughly 30–80 versions, i.e. **~0.1–0.3 MB per run compressed** (~1–5 MB raw). That is
   negligible next to the run's own video (~5–20 MB). Full snapshots buy trivial resume, time-travel
   inspection and `fork --from-version` with no replay logic.
7. **Media** lives in a content-addressed store (`cas/<sha[:2]>/<sha><ext>`) with atomic writes
   (temp file + fsync + rename). The generation cache maps request hash → artifact, and a row
   whose file is missing is a self-healing miss.

## Alternatives considered
- **Event sourcing (store only events, rebuild state):** elegant audit trail, but replay logic,
  upcasting and snapshots-for-speed are a lot of machinery for a prototype. We keep events as the
  *trace* and snapshots as the *state*.
- **JSON diffs/patches between versions:** ~10× smaller again, but complicates load and corruption
  recovery. Not needed at ~3 KB/version.
- **Per-run asyncio lock + in-memory state with periodic saves:** fragile across processes and
  crashes.
- **Postgres:** the right call for multi-user production, unnecessary for a single-node prototype.
  The repository interface keeps the swap contained.
- **SQLAlchemy/aiosqlite:** a few lines saved but more to explain. Operations take well under a
  millisecond, so running sync calls on the event loop (or `asyncio.to_thread` from tasks) is
  acceptable.

## Consequences
- + Atomic state+event commits, gap-free seqs, no lost updates, resumable from any version.
- + Concurrency is tested with two connections × 4 threads × 25 writes (200 entries, versions
  1..201 contiguous, seqs 1..200 gap-free).
- − Every write re-validates and re-serialises the whole aggregate (~1 ms for a typical run).
  Fine at our write rate; the first thing to revisit at scale.
- − Snapshots grow linearly with transitions. Future pruning: keep stage-boundary snapshots, thin
  intra-stage ones older than N.
