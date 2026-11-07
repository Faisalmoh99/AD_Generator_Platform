"""Seed the cross-run motif ledger (ADR-0028) with the winners of the five planning runs of
2026-11-07, which were made before concepts carried motifs (local, $0, no API call).

    uv run python scripts/seed_motifs.py          # writes data/adsmith.db (table motifs)
    uv run python scripts/seed_motifs.py --show   # prints the ledger
"""

from __future__ import annotations

import argparse

from adsmith.agent.context import AgentRuntime
from adsmith.config import Settings
from adsmith.domain.concept import MotifEntry

# run id → (axis, motifs): read from each run's winning concept by hand (titles come from the run).
SEED: dict[str, tuple[str, list[str]]] = {
    "run_01M6SSVMPBEAY1WW1FWF4DGWRZ": ("hospitality", ["hostess", "guests", "front door"]),
    "run_01M6SV0XZVPCFS6XA9HD7EE71M": ("hospitality", ["hostess", "doorbell", "front door"]),
    "run_01M6SVSK8GD53SYGZ8SF7AA9TT": ("hospitality", ["guest", "host", "finjan"]),
    "run_01M6T075QBXT7VV9EW7K5V4MNC": ("occasion", ["grandmother", "gift box", "dates"]),
    "run_01M6T1CVKN10GHKGPZD3JFFQ23": ("hospitality", ["host", "guest", "door"]),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    rt = AgentRuntime.from_settings(Settings())
    if not args.show:
        for run_id, (axis, motifs) in SEED.items():
            state = rt.storage.runs.load(run_id)
            c = state.concepts.selected if state.concepts else None
            title = c.title if c else run_id
            rt.storage.motifs.record(
                MotifEntry(run_id=run_id, title=title[:120], axis=axis, motifs=motifs),
                source="seed",
            )
    for e in rt.storage.motifs.recent(20):
        print(f"{e.run_id}  [{e.axis}]  {', '.join(e.motifs)}  ·  {e.title}")


if __name__ == "__main__":
    main()
