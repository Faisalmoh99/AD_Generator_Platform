"""Account-wide spend ledger for real media providers (fal): the hard money ceiling.

The run budget (``BudgetLedger``) protects one run. This protects the *wallet*: a prepaid
balance shared by every run, data directory, script and process on this machine. It lives in its
own SQLite file (``Settings.account_db``) so it does not split when runs use different data dirs.

Protocol per paid call (all in ``BEGIN IMMEDIATE`` transactions, so concurrent processes cannot
jointly overshoot):

    reserve(estimate)  → refused if estimate > balance − settled − open reservations
    settle(id, actual) → the call succeeded; the actual cost is booked
    release(id)        → the call failed before producing a billable result

A reservation left open by a crashed process keeps counting against the balance (fail-safe)
until ``release_stale`` is run explicitly.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adsmith.domain.base import new_id
from adsmith.domain.budget import USD_DECIMALS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    id          TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    ref         TEXT NOT NULL,             -- e.g. run/scene/phase label
    est_usd     REAL NOT NULL,
    actual_usd  REAL,                      -- NULL while reserved
    state       TEXT NOT NULL,             -- reserved | settled | released
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    remote_id   TEXT                       -- provider request id, for reconciliation
);
"""


class AccountLimitError(Exception):
    """A paid call would exceed the remaining account balance. No call was made."""


@dataclass(frozen=True, slots=True)
class AccountSummary:
    balance_usd: float
    settled_usd: float
    reserved_usd: float
    calls: int

    @property
    def remaining_usd(self) -> float:
        return round(self.balance_usd - self.settled_usd - self.reserved_usd, USD_DECIMALS)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpendAccount:
    def __init__(self, path: Path, *, balance_usd: float, provider: str = "fal") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.balance_usd, self.provider = path, balance_usd, provider
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _summary(self, conn: sqlite3.Connection) -> AccountSummary:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual_usd END), 0) AS settled,"
            " COALESCE(SUM(CASE WHEN state='reserved' THEN est_usd END), 0) AS reserved,"
            " SUM(CASE WHEN state='settled' THEN 1 ELSE 0 END) AS calls"
            " FROM spend WHERE provider = ?",
            (self.provider,),
        ).fetchone()
        return AccountSummary(
            balance_usd=self.balance_usd,
            settled_usd=round(float(row["settled"]), USD_DECIMALS),
            reserved_usd=round(float(row["reserved"]), USD_DECIMALS),
            calls=int(row["calls"] or 0),
        )

    def summary(self) -> AccountSummary:
        with self._tx() as conn:
            return self._summary(conn)

    def reserve(self, *, endpoint: str, est_usd: float, ref: str) -> str:
        with self._tx() as conn:
            s = self._summary(conn)
            if est_usd > s.remaining_usd + 1e-9:
                raise AccountLimitError(
                    f"{self.provider} call refused: estimate ${est_usd:.3f} exceeds the remaining "
                    f"account balance ${s.remaining_usd:.3f} (balance ${s.balance_usd:.2f}, "
                    f"spent ${s.settled_usd:.3f}, reserved ${s.reserved_usd:.3f})"
                )
            sid = new_id("spend")
            now = _now()
            conn.execute(
                "INSERT INTO spend (id, provider, endpoint, ref, est_usd, state, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)",
                (sid, self.provider, endpoint, ref, est_usd, now, now),
            )
            return sid

    def settle(self, spend_id: str, actual_usd: float, *, remote_id: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE spend SET state='settled', actual_usd=?, remote_id=?, updated_at=?"
                " WHERE id=? AND state='reserved'",
                (actual_usd, remote_id, _now(), spend_id),
            )

    def release(self, spend_id: str, *, remote_id: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE spend SET state='released', remote_id=COALESCE(?, remote_id),"
                " updated_at=? WHERE id=? AND state='reserved'",
                (remote_id, _now(), spend_id),
            )

    def release_stale(self, older_than: timedelta) -> int:
        """Explicit operator action: free reservations left by crashed processes."""
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE spend SET state='released', updated_at=? "
                "WHERE state='reserved' AND created_at < ?",
                (_now(), cutoff),
            )
            return cur.rowcount

    def entries(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM spend ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
