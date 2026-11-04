from __future__ import annotations

from pathlib import Path

import pytest

from adsmith.agent.prompts import PromptLibrary, PromptTemplateError, parse_template
from adsmith.agent.schemas import OUTPUT_SCHEMAS
from adsmith.config import PROJECT_ROOT


def write(root: Path, name: str, body: str, version: int = 1) -> Path:
    path = root / name / f"v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


GOOD = """---
name: demo
version: 1
output_schema: BriefDraft
---
=== SYSTEM ===
Static rules.
=== CONTEXT ===
Bible: {{ bible }}
=== USER ===
Request: {{ request }}
"""


def test_render_sections_and_variables(tmp_path: Path) -> None:
    t = parse_template(write(tmp_path, "demo", GOOD))
    assert t.template_id == "demo@v1" and t.variables == {"bible", "request"}
    r = t.render(bible="B", request="R")
    assert (r.system, r.context, r.user) == ("Static rules.", "Bible: B", "Request: R")


def test_missing_variable_is_an_error(tmp_path: Path) -> None:
    t = parse_template(write(tmp_path, "demo", GOOD))
    with pytest.raises(PromptTemplateError, match="missing variables"):
        t.render(request="R")


def test_system_must_be_static(tmp_path: Path) -> None:
    body = GOOD.replace("Static rules.", "Rules for {{ brand }}.")
    with pytest.raises(PromptTemplateError, match="SYSTEM must be static"):
        parse_template(write(tmp_path, "demo", body))


def test_name_and_version_must_match_path(tmp_path: Path) -> None:
    with pytest.raises(PromptTemplateError, match="must match the path"):
        parse_template(write(tmp_path, "other", GOOD))
    with pytest.raises(PromptTemplateError, match="must match the path"):
        parse_template(write(tmp_path, "demo", GOOD, version=2))


def test_latest_version_is_default(tmp_path: Path) -> None:
    write(tmp_path, "demo", GOOD)
    write(tmp_path, "demo", GOOD.replace("version: 1", "version: 2").replace("Static", "V2"), 2)
    lib = PromptLibrary(tmp_path)
    assert lib.get("demo").version == 2 and lib.get("demo", 1).system == "Static rules."


def test_repository_templates_are_valid_and_registered() -> None:
    lib = PromptLibrary(PROJECT_ROOT / "prompts")
    templates = lib.all_latest()
    assert templates, "expected at least the schema_repair fragment"
    for t in templates:
        if t.output_schema is not None:
            assert t.name in OUTPUT_SCHEMAS, f"{t.template_id}: schema not in the registry"
            assert OUTPUT_SCHEMAS[t.name].__name__ == t.output_schema
