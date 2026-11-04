"""Content-addressed media store.

Layout under ``data_dir``:
    cas/<sha[:2]>/<sha><ext>     immutable media, named by content hash (dedupes automatically)
    runs/<run_id>/...            per-run outputs (final.mp4, production_pack/, scratch)

Writes are atomic: copy to a temp file in the destination directory, fsync, then ``os.replace``.
A crash mid-write never leaves a half-written file under a valid hash name.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from adsmith.domain.generation import Artifact

_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir
        self.cas_dir = data_dir / "cas"
        self.runs_dir = data_dir / "runs"
        self.cas_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        path = self.runs_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def scratch_dir(self, run_id: str) -> Path:
        path = self.run_dir(run_id) / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ingest(
        self,
        src: Path,
        *,
        kind: Literal["image", "video", "audio"],
        provider: str,
        width: int | None = None,
        height: int | None = None,
        duration_s: float | None = None,
        remote_url: str | None = None,
        move: bool = False,
    ) -> Artifact:
        """Store ``src`` under its content hash and return a typed ``Artifact``. Idempotent."""
        sha = sha256_file(src)
        dest = self.cas_dir / sha[:2] / f"{sha}{src.suffix.lower()}"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".ingest-")
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
                    shutil.copyfileobj(inp, out, _CHUNK)
                    out.flush()
                    os.fsync(out.fileno())
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
        if move and src.resolve() != dest.resolve():
            src.unlink(missing_ok=True)
        return Artifact(
            kind=kind,
            path=dest.relative_to(self.root).as_posix(),
            sha256=sha,
            width=width,
            height=height,
            duration_s=duration_s,
            provider=provider,
            remote_url=remote_url,
        )

    def resolve(self, artifact: Artifact) -> Path:
        path = (self.root / artifact.path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"artifact path escapes the data dir: {artifact.path}")
        return path

    def exists(self, artifact: Artifact, *, verify_hash: bool = False) -> bool:
        path = self.resolve(artifact)
        if not path.is_file() or path.stat().st_size == 0:
            return False
        return not verify_hash or sha256_file(path) == artifact.sha256
