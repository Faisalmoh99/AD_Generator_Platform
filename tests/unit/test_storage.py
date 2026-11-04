"""Storage: snapshots, atomic updates, optimistic locking, gap-free event seqs, CAS and cache."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from adsmith.domain.budget import BudgetEntry
from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState
from adsmith.storage import (
    ConcurrencyConflictError,
    Database,
    RunNotFoundError,
    RunRepository,
    StaleWriteError,
    Storage,
)
from adsmith.storage.db import MIGRATIONS
from adsmith.storage.events import EventStore
from adsmith.storage.repo import Mutation
from tests.factories import make_brief, make_full_run, make_run


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[Storage]:
    s = Storage.open(tmp_path / "data")
    yield s
    s.db.close()


def ev(run_id: str, summary: str = "x", stage: Stage = Stage.UNDERSTAND) -> AgentEvent:
    return AgentEvent(run_id=run_id, stage=stage, type=EventType.DECISION, summary=summary)


def add_entry(ref: str) -> Mutation:
    def mutate(s: RunState) -> Sequence[AgentEvent]:
        s.budget.entries.append(BudgetEntry(category="llm", est_usd=0.01, ref=ref))
        return [ev(s.id, f"entry {ref}")]

    return mutate


# ----------------------------------------------------------------------------- schema


def test_migrations_apply_once(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    assert db.schema_version() == len(MIGRATIONS)
    db.close()
    db2 = Database(tmp_path / "a.db")  # reopening must not re-run migrations
    assert db2.schema_version() == len(MIGRATIONS)
    db2.close()


# ----------------------------------------------------------------------------- snapshots


def test_create_and_load_round_trip(storage: Storage) -> None:
    run = make_run()
    created = storage.runs.create(run, events=[ev(run.id, "created")])
    assert created.state.state_version == 1
    assert created.events[0].seq == 1
    loaded = storage.runs.load(run.id)
    assert loaded == created.state


def test_unknown_run(storage: Storage) -> None:
    with pytest.raises(RunNotFoundError):
        storage.runs.load("run_nope")


def test_update_bumps_version_and_keeps_history(storage: Storage) -> None:
    run = storage.runs.create(make_run()).state

    def set_brief(s: RunState) -> None:
        s.brief = make_brief()
        s.stage = Stage.IDEATE

    res = storage.runs.update(run.id, set_brief, reason="understand done")
    assert res.state.state_version == 2 and res.state.stage is Stage.IDEATE
    hist = storage.runs.history(run.id)
    assert [(h.state_version, h.stage, h.reason) for h in hist] == [
        (1, Stage.UNDERSTAND, "created"),
        (2, Stage.IDEATE, "understand done"),
    ]
    assert storage.runs.load_version(run.id, 1).brief is None  # past versions are inspectable


def test_failed_mutation_rolls_back_everything(storage: Storage) -> None:
    run = storage.runs.create(make_run()).state

    def boom(s: RunState) -> Sequence[AgentEvent]:
        s.stage = Stage.IDEATE
        raise RuntimeError("node crashed mid-apply")

    with pytest.raises(RuntimeError):
        storage.runs.update(run.id, boom, reason="x")
    assert storage.runs.load(run.id).state_version == 1
    assert storage.events.last_seq(run.id) == 0
    assert len(storage.runs.history(run.id)) == 1


def test_invalid_resulting_state_is_never_stored(storage: Storage) -> None:
    run = storage.runs.create(make_run()).state

    def skip_ahead(s: RunState) -> None:
        s.stage = Stage.STORYBOARD  # violates stage prerequisites (no brief/concepts)

    with pytest.raises(ValidationError):
        storage.runs.update(run.id, skip_ahead, reason="bad")
    assert storage.runs.load(run.id).stage is Stage.UNDERSTAND


def test_rollback_consumes_no_event_seq(storage: Storage) -> None:
    run = storage.runs.create(make_run()).state
    storage.runs.update(run.id, add_entry("a"), reason="a")  # seq 1

    def veto(s: RunState) -> Sequence[AgentEvent]:
        raise StaleWriteError("precondition failed")

    with pytest.raises(StaleWriteError):
        storage.runs.update(run.id, veto, reason="v")
    res = storage.runs.update(run.id, add_entry("b"), reason="b")
    assert [e.seq for e in res.events] == [2]  # no gap left by the rolled-back write


# ----------------------------------------------------------------------------- concurrency


def test_optimistic_lock_rejects_stale_human_command(storage: Storage) -> None:
    run = storage.runs.create(make_run()).state
    seen = storage.runs.load(run.id).state_version  # user looked at v1
    storage.runs.update(run.id, add_entry("agent"), reason="agent progressed")  # now v2
    with pytest.raises(ConcurrencyConflictError) as exc:
        storage.runs.update(run.id, add_entry("user"), reason="edit", expected_version=seen)
    assert (exc.value.expected, exc.value.actual) == (1, 2)


def test_no_lost_updates_and_gap_free_seqs_across_connections(tmp_path: Path) -> None:
    """Two connections (≈ two processes) × 4 threads × 25 writes against the same run."""
    path = tmp_path / "shared.db"
    dbs = [Database(path), Database(path)]
    repos = [RunRepository(db) for db in dbs]
    run_id = repos[0].create(make_run()).state.id
    errors: list[Exception] = []

    def worker(repo: RunRepository, tag: str) -> None:
        try:
            for i in range(25):
                repo.update(run_id, add_entry(f"{tag}-{i}"), reason=tag)
        except Exception as exc:  # noqa: BLE001 - threads can't raise to us; asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(repos[t % 2], f"t{t}")) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    final = repos[1].load(run_id)
    refs = [e.ref for e in final.budget.entries]
    assert len(refs) == 200 and len(set(refs)) == 200  # every write landed exactly once
    assert final.state_version == 201
    assert [h.state_version for h in repos[0].history(run_id)] == list(range(1, 202))
    seqs = [e.seq for e in EventStore(dbs[0]).tail(run_id, limit=1000)]
    assert seqs == list(range(1, 201))  # strictly monotonic, gap-free
    for db in dbs:
        db.close()


async def test_concurrent_asyncio_appends_are_gap_free(storage: Storage) -> None:
    run_id = storage.runs.create(make_run()).state.id

    async def scene_task(n: int) -> None:
        for i in range(10):
            await asyncio.to_thread(storage.events.append, run_id, [ev(run_id, f"s{n}-{i}")])

    await asyncio.gather(*(scene_task(n) for n in range(6)))
    assert [e.seq for e in storage.events.tail(run_id, limit=100)] == list(range(1, 61))


def test_stale_scene_result_is_vetoed_not_misapplied(storage: Storage) -> None:
    """A result computed for an old run version is checked against the *current* state inside
    the write transaction. The mutation itself decides to veto (StaleWriteError)."""
    run = storage.runs.create(make_run()).state
    observed = len(storage.runs.load(run.id).budget.entries)
    storage.runs.update(run.id, add_entry("concurrent edit"), reason="edit")

    def apply_result(s: RunState) -> None:
        if len(s.budget.entries) != observed:
            raise StaleWriteError("scene was edited while the result was being produced")

    with pytest.raises(StaleWriteError):
        storage.runs.update(run.id, apply_result, reason="scene result")


# ----------------------------------------------------------------------------- events


def test_tail_after_seq_and_limit(storage: Storage) -> None:
    run_id = storage.runs.create(make_run()).state.id
    storage.events.append(run_id, [ev(run_id, f"e{i}") for i in range(10)])
    assert [e.seq for e in storage.events.tail(run_id, after_seq=7)] == [8, 9, 10]
    assert [e.seq for e in storage.events.tail(run_id, after_seq=0, limit=3)] == [1, 2, 3]
    assert storage.events.tail(run_id, after_seq=10) == []


def test_event_for_other_run_rejected(storage: Storage) -> None:
    run_id = storage.runs.create(make_run()).state.id
    with pytest.raises(ValueError, match="appended to run"):
        storage.events.append(run_id, [ev("run_other")])


def test_event_payload_round_trips_unicode(storage: Storage) -> None:
    run_id = storage.runs.create(make_run()).state.id
    e = AgentEvent(
        run_id=run_id,
        stage=Stage.UNDERSTAND,
        type=EventType.DECISION,
        summary="فهمت الطلب: إعلان عطر",
        payload={"dialect": "saudi_najdi", "terms": ["أبغى", "خل"]},
    )
    (stored,) = storage.events.append(run_id, [e])
    (loaded,) = storage.events.tail(run_id)
    assert loaded == stored


# ----------------------------------------------------------------------------- fork


def test_fork_from_past_version(storage: Storage) -> None:
    run = storage.runs.create(make_run()).state

    def to_ideate(s: RunState) -> None:
        s.brief, s.stage = make_brief(), Stage.IDEATE

    storage.runs.update(run.id, to_ideate, reason="understand")
    forked = storage.runs.fork(run.id, from_version=1)
    assert forked.state.id != run.id and forked.state.stage is Stage.UNDERSTAND
    assert forked.state.state_version == 1
    assert forked.events[0].payload["forked_from"] == run.id
    summary = next(r for r in storage.runs.list_runs() if r.id == forked.state.id)
    assert summary.forked_from == run.id


# ----------------------------------------------------------------------------- size


def test_snapshot_size_stays_small(storage: Storage) -> None:
    run = make_full_run(Stage.PROMPT)
    storage.runs.create(run)
    (info,) = storage.runs.history(run.id)
    assert info.size_bytes < 8 * 1024  # compressed; ~16 KB raw


# ----------------------------------------------------------------------------- artifacts


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_ingest_is_content_addressed_and_idempotent(storage: Storage, tmp_path: Path) -> None:
    a = storage.artifacts.ingest(_write(tmp_path / "a.PNG", b"img"), kind="image", provider="mock")
    b = storage.artifacts.ingest(_write(tmp_path / "b.png", b"img"), kind="image", provider="mock")
    assert (
        a.path == b.path and a.path.startswith(f"cas/{a.sha256[:2]}/") and a.path.endswith(".png")
    )
    assert storage.artifacts.exists(a, verify_hash=True)


def test_ingest_move_removes_source(storage: Storage, tmp_path: Path) -> None:
    src = _write(tmp_path / "clip.mp4", b"video")
    art = storage.artifacts.ingest(src, kind="video", provider="mock", move=True)
    assert not src.exists() and storage.artifacts.resolve(art).read_bytes() == b"video"


def test_resolve_refuses_path_escape(storage: Storage, tmp_path: Path) -> None:
    art = storage.artifacts.ingest(_write(tmp_path / "x.png", b"x"), kind="image", provider="m")
    evil = art.model_copy(update={"path": "../../etc/passwd"})
    with pytest.raises(ValueError, match="escapes"):
        storage.artifacts.resolve(evil)


# ----------------------------------------------------------------------------- cache


def test_cache_hit_miss_and_first_writer_wins(storage: Storage, tmp_path: Path) -> None:
    a = storage.artifacts.ingest(_write(tmp_path / "a.png", b"A"), kind="image", provider="m")
    b = storage.artifacts.ingest(_write(tmp_path / "b.png", b"B"), kind="image", provider="m")
    assert storage.cache.get("k") is None
    storage.cache.put("k", a)
    storage.cache.put("k", b)
    assert storage.cache.get("k") == a
    assert storage.cache.hits("k") == 1


def test_cache_self_heals_when_file_missing(storage: Storage, tmp_path: Path) -> None:
    a = storage.artifacts.ingest(_write(tmp_path / "a.png", b"A"), kind="image", provider="m")
    storage.cache.put("k", a)
    storage.artifacts.resolve(a).unlink()
    assert storage.cache.get("k") is None  # treated as a miss
    storage.cache.put("k", a)  # and the stale row was removed, so a new put works
    assert storage.cache.hits("k") == 0
