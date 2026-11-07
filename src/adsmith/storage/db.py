"""SQLite connection management and schema migrations.

Concurrency model (ADR-0011):
- One ``Database`` per process, one connection, guarded by a re-entrant lock. Agent scene tasks run
  as asyncio tasks in one event loop, and FastAPI may call from its threadpool. The lock
  serialises them; individual operations take well under a millisecond.
- WAL mode so other processes (e.g. ``adsmith trace`` while ``serve`` runs) can read during a
  write. Cross-process writers are serialised by SQLite's write lock (``BEGIN IMMEDIATE``) with a
  busy timeout.
- We manage transactions explicitly (``isolation_level=None``): every write happens inside
  ``Database.write()``, which is all-or-nothing.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_MS = 5000

# Append-only list: index + 1 is the schema version. Never edit a shipped migration.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE runs (
        id             TEXT PRIMARY KEY,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        stage          TEXT NOT NULL,
        state_version  INTEGER NOT NULL,
        request        TEXT NOT NULL,
        forked_from    TEXT
    );
    CREATE TABLE snapshots (
        run_id         TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        state_version  INTEGER NOT NULL,
        stage          TEXT NOT NULL,
        reason         TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        state_z        BLOB NOT NULL,          -- zlib-compressed RunState JSON
        PRIMARY KEY (run_id, state_version)
    ) WITHOUT ROWID;
    CREATE TABLE events (
        run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        seq       INTEGER NOT NULL,
        ts        TEXT NOT NULL,
        stage     TEXT NOT NULL,
        scene_id  TEXT,
        type      TEXT NOT NULL,
        level     TEXT NOT NULL,
        summary   TEXT NOT NULL,
        payload   TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    ) WITHOUT ROWID;
    CREATE TABLE cache (
        key         TEXT PRIMARY KEY,
        artifact    TEXT NOT NULL,             -- Artifact JSON
        created_at  TEXT NOT NULL,
        hits        INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 2: the cross-run motif ledger (ADR-0028 part 3): what recent winning concepts stood on.
    """
    CREATE TABLE motifs (
        run_id      TEXT PRIMARY KEY,
        created_at  TEXT NOT NULL,
        title       TEXT NOT NULL,
        axis        TEXT NOT NULL,
        motifs      TEXT NOT NULL,             -- JSON list of short English nouns
        source      TEXT NOT NULL              -- run | seed
    );
    """,
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA foreign_keys=ON",
            f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}",
        ):
            self._conn.execute(pragma)
        self._migrate()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction. Commits on success, rolls back on any exception."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def schema_version(self) -> int:
        with self.read() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0])

    def _migrate(self) -> None:
        current = self.schema_version()
        for version, script in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            with self._lock:
                # executescript commits implicitly; user_version is set in the same script.
                self._conn.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
