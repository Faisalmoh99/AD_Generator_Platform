"""Proves writes take SQLite's write lock *before* reading (BEGIN IMMEDIATE), not at first write.

Under a deferred ``BEGIN``, a second connection's transaction would start immediately and could
read ``MAX(seq)`` / the latest snapshot while the first writer is still mid-transaction, which is
exactly the read-then-write race the repository relies on SQLite to prevent. The control test runs
the same scenario with a deferred-BEGIN subclass and shows the detector catches it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from adsmith.storage.db import Database

_HOLD_S = 0.3


class DeferredBeginDatabase(Database):
    """What we must NOT do: plain BEGIN acquires the write lock lazily."""

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")


def _b_entered_while_a_held(db_a: Database, db_b: Database) -> bool:
    """A holds a write txn; B tries to open one. Did B's body run before A committed?"""
    a_inside, b_entered = threading.Event(), threading.Event()
    a_committed = threading.Event()
    entered_early: list[bool] = []

    def writer_b() -> None:
        a_inside.wait()
        with db_b.write() as conn:
            entered_early.append(not a_committed.is_set())
            b_entered.set()
            conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = 'r'")

    t = threading.Thread(target=writer_b)
    t.start()
    with db_a.write() as conn:
        conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = 'r'")
        a_inside.set()
        time.sleep(_HOLD_S)  # give B every chance to sneak in
    a_committed.set()
    t.join(timeout=10)
    assert b_entered.is_set(), "B never ran"
    return entered_early[0]


def test_second_writer_blocks_until_first_commits(tmp_path: Path) -> None:
    path = tmp_path / "lock.db"
    db_a, db_b = Database(path), Database(path)
    try:
        assert not _b_entered_while_a_held(db_a, db_b)
    finally:
        db_a.close()
        db_b.close()


def test_control_deferred_begin_would_race(tmp_path: Path) -> None:
    """Sensitivity check: the detector above does catch the deferred variant."""
    path = tmp_path / "lock.db"
    Database(path).close()  # run migrations
    db_a, db_b = DeferredBeginDatabase(path), DeferredBeginDatabase(path)
    try:
        assert _b_entered_while_a_held(db_a, db_b)
    finally:
        db_a.close()
        db_b.close()


def test_pragmas(tmp_path: Path) -> None:
    db = Database(tmp_path / "p.db")
    with db.read() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.close()
