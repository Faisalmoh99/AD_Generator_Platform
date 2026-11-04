"""Turn a real run into a scripted-LLM scenario (dev tool).

    uv run python scripts/harvest_fixtures.py <data_dir> <run_id> <scenario_name>

Reconstructs each node's *draft* output from the committed state (and the select decision event),
so the zero-key Quickstart replays genuine model output. The fixture-integrity test validates the
result against the schemas and through the real domain conversions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.providers.llm.scripted import FIXTURES_DIR
from adsmith.storage import Storage

SCENE_DRAFT = {"purpose", "description", "duration_s", "shot", "shows_character", "shows_product"}


def harvest(data_dir: Path, run_id: str, scenario: str) -> Path:
    storage = Storage.open(data_dir)
    s = storage.runs.load(run_id)
    assert s.brief and s.concepts and s.storyboard and s.bible, "run must have completed planning"
    scores: list[dict[str, Any]] = []
    for e in storage.events.tail(run_id, limit=10_000):
        if e.stage is Stage.SELECT and e.type is EventType.DECISION and "scores" in e.payload:
            raw = e.payload["scores"]
            assert isinstance(raw, list)
            scores = [{k: v for k, v in dict(x).items() if k != "weighted"} for x in raw]  # type: ignore[arg-type]
    outputs: dict[str, list[dict[str, Any]]] = {
        "understand": [{"output": s.brief.model_dump(mode="json", exclude={"id", "raw_request"})}],
        "ideate": [
            {
                "output": {
                    "category_cliches": s.concepts.category_cliches,
                    "concepts": [
                        c.model_dump(mode="json", exclude={"id"}) for c in s.concepts.concepts
                    ],
                }
            }
        ],
        "select": [{"output": {"scores": scores}}],
        "storyboard": [
            {
                "output": {
                    "scenes": [
                        sc.model_dump(mode="json", include=SCENE_DRAFT)
                        for sc in s.storyboard.scenes
                    ]
                }
            }
        ],
        "continuity": [{"output": s.bible.model_dump(mode="json", exclude={"version"})}],
        "prompt_writer": [
            {
                "output": sc.prompt.model_dump(
                    mode="json", include={"keyframe_prompt", "motion_prompt", "negative_prompt"}
                )
            }
            for sc in s.storyboard.scenes
            if sc.prompt
        ],
    }
    out = FIXTURES_DIR / scenario
    out.mkdir(parents=True, exist_ok=True)
    for name, steps in outputs.items():
        (out / f"{name}.json").write_text(
            json.dumps(steps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return out


if __name__ == "__main__":
    print(harvest(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
