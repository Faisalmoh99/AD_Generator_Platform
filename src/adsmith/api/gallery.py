"""Real-runs gallery: curated real fal outputs (config/real_runs.toml) resolved to media URLs."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from adsmith.api.view import media_url
from adsmith.domain.enums import ScenePhase
from adsmith.storage import RunNotFoundError, Storage


def load_gallery(config_dir: Path, storage: Storage) -> list[dict[str, Any]]:
    path = config_dir / "real_runs.toml"
    if not path.is_file():
        return []
    with path.open("rb") as fh:
        items = tomllib.load(fh).get("items", [])
    out = []
    for item in items:
        try:
            state = storage.runs.load(item["run"])
        except RunNotFoundError:
            # Fresh clone: data/ is gitignored, but the clips are committed under docs/media.
            if fallback := _committed(item, config_dir.parent / "docs" / "media"):
                out.append(fallback)
            continue
        videos: list[dict[str, Any]] = []
        # kind "final": the assembled ad; "attempts": chosen clip attempts of one scene;
        # "hero": the assembled ad followed by the best-of-N candidate clips (labels/posters).
        kind = item.get("kind")
        if kind in {"final", "hero"} and state.final is not None:
            videos.append(
                {
                    "url": media_url(state.final.path),
                    "label": "Final ad",
                    "poster": item.get("poster"),
                }
            )
        if kind in {"attempts", "hero"} and state.storyboard is not None:
            scene = state.storyboard.scenes[int(item["scene"]) - 1]
            labels = item.get("labels", [])
            for n, attempt_no in enumerate(item.get("attempts", [])):
                att = next(
                    (
                        a
                        for a in scene.attempts
                        if a.attempt_no == attempt_no and a.phase is ScenePhase.CLIP and a.artifact
                    ),
                    None,
                )
                if att is not None and att.artifact is not None:
                    posters = item.get("posters", [])
                    videos.append(
                        {
                            "url": media_url(att.artifact.path),
                            "label": labels[n] if n < len(labels) else f"Attempt {attempt_no}",
                            "poster": posters[n] if n < len(posters) else None,
                        }
                    )
        if not videos:
            continue
        out.append(
            {
                "key": item["key"],
                "title": item["title"],
                "subtitle": item.get("subtitle", ""),
                "caption": item.get("caption", ""),
                "defects": item.get("defects", ""),
                "videos": videos,
                "endcard": state.endcard.model_dump(mode="json") if state.endcard else None,
                "spend": round(
                    state.budget.by_category.get("video", 0.0)
                    + state.budget.by_category.get("image", 0.0)
                    + state.budget.by_category.get("audio", 0.0),
                    3,
                ),
            }
        )
    return out


def _committed(item: dict[str, Any], media_dir: Path) -> dict[str, Any] | None:
    """A gallery card built from the committed copies (``media`` = file names in docs/media)."""
    names = [n for n in item.get("media", []) if (media_dir / n).is_file()]
    if not names:
        return None
    labels, posters = item.get("labels", ["Final ad"]), item.get("posters", [item.get("poster")])
    return {
        "key": item["key"],
        "title": item["title"],
        "subtitle": item.get("subtitle", ""),
        "caption": item.get("caption", ""),
        "defects": item.get("defects", ""),
        "videos": [
            {
                "url": f"/docs-media/{n}",
                "label": labels[i] if i < len(labels) else "",
                "poster": posters[i] if i < len(posters) else None,
            }
            for i, n in enumerate(names)
        ],
        "endcard": None,
        "spend": item.get("spend"),
    }
