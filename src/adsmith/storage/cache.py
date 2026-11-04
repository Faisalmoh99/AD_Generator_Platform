"""Generation cache: ``GenerationRequest.cache_key()`` → stored ``Artifact``.

Only successes are cached. A cache entry whose file has disappeared is treated as a miss and
removed (self-healing), so a cleaned ``cas/`` can never make the agent return a dangling path.
"""

from __future__ import annotations

from adsmith.domain.base import utcnow
from adsmith.domain.generation import Artifact
from adsmith.storage.artifacts import ArtifactStore
from adsmith.storage.db import Database


class CacheStore:
    def __init__(self, db: Database, artifacts: ArtifactStore) -> None:
        self._db = db
        self._artifacts = artifacts

    def get(self, key: str) -> Artifact | None:
        with self._db.read() as conn:
            row = conn.execute("SELECT artifact FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        artifact = Artifact.model_validate_json(row["artifact"])
        if not self._artifacts.exists(artifact):
            with self._db.write() as conn:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            return None
        with self._db.write() as conn:
            conn.execute("UPDATE cache SET hits = hits + 1 WHERE key = ?", (key,))
        return artifact

    def put(self, key: str, artifact: Artifact) -> None:
        """First writer wins: identical requests produce interchangeable results."""
        with self._db.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO cache (key, artifact, created_at) VALUES (?, ?, ?)",
                (key, artifact.model_dump_json(), utcnow().isoformat()),
            )

    def evict(self, key: str) -> bool:
        """Forget a result that turned out to be bad (e.g. rejected by QA), so an identical
        request is generated again instead of returning the known-bad artifact."""
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        return cur.rowcount > 0

    def hits(self, key: str) -> int:
        with self._db.read() as conn:
            row = conn.execute("SELECT hits FROM cache WHERE key = ?", (key,)).fetchone()
        return 0 if row is None else int(row["hits"])
