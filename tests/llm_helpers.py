"""Helpers to build a StructuredCaller over a scripted provider with instant, recorded sleeps."""

from __future__ import annotations

import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adsmith.agent.prompts import PromptLibrary
from adsmith.agent.structured import StructuredCaller
from adsmith.config import PROJECT_ROOT, Settings
from adsmith.providers.llm.scripted import ScriptedLLMProvider

NODE_TEMPLATE = """---
name: {name}
version: 1
output_schema: {schema}
---
=== SYSTEM ===
You are a test node. Static instructions only.
=== CONTEXT ===
Shared context: {{{{ shared }}}}
=== USER ===
Task: {{{{ task }}}}
"""


def make_prompt_library(root: Path) -> PromptLibrary:
    shutil.copytree(PROJECT_ROOT / "prompts" / "schema_repair", root / "schema_repair")
    for name, schema in [
        ("ideate", "CreativeConceptSetDraft"),
        ("understand", "BriefDraft"),
        ("storyboard", "StoryboardDraft"),
    ]:
        (root / name).mkdir(parents=True)
        (root / name / "v1.md").write_text(NODE_TEMPLATE.format(name=name, schema=schema))
    return PromptLibrary(root)


@dataclass
class Harness:
    caller: StructuredCaller
    provider: ScriptedLLMProvider
    prompts: PromptLibrary
    sleeps: list[float] = field(default_factory=list)


def make_harness(
    settings: Settings, tmp_path: Path, scripts: dict[str, list[dict[str, Any]]]
) -> Harness:
    prompts = make_prompt_library(tmp_path / "prompts")
    provider = ScriptedLLMProvider(scripts)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    caller = StructuredCaller(
        provider=provider, settings=settings, prompts=prompts, sleep=sleep, rng=random.Random(0)
    )
    return Harness(caller=caller, provider=provider, prompts=prompts, sleeps=sleeps)
