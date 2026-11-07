"""Export the human decisions logged at the gates (ADR-0029 part 3) as JSONL, one line per
(preferred, rejected) pair with its kind and context: the shape a pairwise judge is calibrated
or trained on. Local, $0, no API call.

    uv run python scripts/export_preferences.py                 # data/preferences.jsonl
    uv run python scripts/export_preferences.py --out pairs.jsonl --run run_01...
    uv run python scripts/export_preferences.py --summary       # counts only
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from adsmith.config import Settings
from adsmith.storage import Storage


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--run", default=None)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    settings = Settings()
    records = Storage.open(settings.data_dir).preferences.all(run_id=args.run)
    pairs = [
        {"run_id": r.run_id, "kind": r.kind, "preferred": win, "rejected": lose,
         "human_written": r.written is not None, "context": r.context}
        for r in records
        for win, lose in r.pairs()
    ]  # fmt: skip
    print(f"{len(records)} decisions, {len(pairs)} pairs: {dict(Counter(r.kind for r in records))}")
    agreed = [r for r in records if r.chosen is not None and r.recommended is not None]
    if agreed:
        same = sum(r.chosen == r.recommended for r in agreed)
        print(f"the human agreed with the system's pick {same} of {len(agreed)} times")
    if args.summary:
        return
    out = args.out or settings.data_dir / "preferences.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for p in pairs:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
