"""Versioned prompt templates: ``prompts/<name>/v<N>.md``.

File format::

    ---
    name: prompt_writer
    version: 1
    kind: node            # node | fragment (fragments have no SYSTEM, e.g. schema_repair)
    output_schema: ScenePromptDraft
    description: ...
    ---
    === SYSTEM ===
    static instructions (NO template variables: this is the cached prefix)
    === CONTEXT ===
    optional: stable per-run context (bible, storyboard), cached as a second breakpoint
    === USER ===
    the variable part, rendered with Jinja2 (StrictUndefined)

Rules enforced at load time: name/version match the path, node templates have a SYSTEM section
with no variables (so the cache prefix is byte-identical across calls by construction), and
undefined variables are errors, never silently empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from jinja2 import Environment, StrictUndefined, Template, meta

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_SECTION = re.compile(r"^=== (SYSTEM|CONTEXT|USER) ===\s*$", re.MULTILINE)
_VERSION_FILE = re.compile(r"^v(\d+)\.md$")


class PromptTemplateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    template_id: str  # "name@vN"
    system: str
    context: str | None
    user: str
    cache_system: bool


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: int
    kind: Literal["node", "fragment"]
    output_schema: str | None
    description: str
    cache_system: bool
    system: str
    context: Template | None
    user: Template
    variables: frozenset[str]

    @property
    def template_id(self) -> str:
        return f"{self.name}@v{self.version}"

    def render(self, **variables: Any) -> RenderedPrompt:
        missing = self.variables - variables.keys()
        if missing:
            raise PromptTemplateError(f"{self.template_id}: missing variables {sorted(missing)}")
        return RenderedPrompt(
            template_id=self.template_id,
            system=self.system,
            context=self.context.render(**variables).strip() if self.context else None,
            user=self.user.render(**variables).strip(),
            cache_system=self.cache_system,
        )


def _env() -> Environment:
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,  # prompts are plain text, not HTML
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def parse_template(path: Path) -> PromptTemplate:
    raw = path.read_text(encoding="utf-8")
    fm = _FRONTMATTER.match(raw)
    if not fm:
        raise PromptTemplateError(f"{path}: missing YAML frontmatter")
    meta_data: dict[str, Any] = yaml.safe_load(fm.group(1)) or {}
    body = raw[fm.end() :]

    name, version = meta_data.get("name"), meta_data.get("version")
    m = _VERSION_FILE.match(path.name)
    if name != path.parent.name or not m or version != int(m.group(1)):
        raise PromptTemplateError(f"{path}: frontmatter name/version must match the path")
    kind = meta_data.get("kind", "node")
    if kind not in {"node", "fragment"}:
        raise PromptTemplateError(f"{path}: kind must be node|fragment")

    parts = _SECTION.split(body)
    sections: dict[str, str] = {}
    for label, content in zip(parts[1::2], parts[2::2], strict=True):
        if label in sections:
            raise PromptTemplateError(f"{path}: duplicate section {label}")
        sections[label] = content.strip()
    if "USER" not in sections:
        raise PromptTemplateError(f"{path}: missing === USER === section")

    env = _env()
    system = sections.get("SYSTEM", "")
    if kind == "node" and not system:
        raise PromptTemplateError(f"{path}: node templates need a SYSTEM section")
    if meta.find_undeclared_variables(env.parse(system)):
        raise PromptTemplateError(
            f"{path}: SYSTEM must be static (no variables) so it is a stable cache prefix"
        )
    variables: set[str] = set(meta.find_undeclared_variables(env.parse(sections["USER"])))
    context_src = sections.get("CONTEXT")
    if context_src:
        variables |= meta.find_undeclared_variables(env.parse(context_src))

    return PromptTemplate(
        name=name,
        version=version,
        kind=kind,
        output_schema=meta_data.get("output_schema"),
        description=str(meta_data.get("description", "")),
        cache_system=bool(meta_data.get("cache_system", True)),
        system=system,
        context=env.from_string(context_src) if context_src else None,
        user=env.from_string(sections["USER"]),
        variables=frozenset(variables),
    )


class PromptLibrary:
    def __init__(self, prompts_dir: Path) -> None:
        self.root = prompts_dir

    def versions(self, name: str) -> list[int]:
        directory = self.root / name
        found = [
            int(m.group(1)) for p in directory.glob("v*.md") if (m := _VERSION_FILE.match(p.name))
        ]
        if not found:
            raise PromptTemplateError(f"no template named {name!r} in {self.root}")
        return sorted(found)

    def get(self, name: str, version: int | None = None) -> PromptTemplate:
        v = version if version is not None else self.versions(name)[-1]
        return _load(self.root / name / f"v{v}.md")

    def all_latest(self) -> list[PromptTemplate]:
        return [self.get(d.name) for d in sorted(self.root.iterdir()) if d.is_dir()]


@cache
def _load(path: Path) -> PromptTemplate:
    if not path.is_file():
        raise PromptTemplateError(f"template not found: {path}")
    return parse_template(path)
