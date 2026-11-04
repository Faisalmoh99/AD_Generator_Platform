"""Deterministic scripted LLM: powers unit tests and Quickstart A (zero keys, ADR-0008).

Each template name maps to an ordered list of steps; each call to that template consumes the next
step, and the last step repeats once the list is exhausted. A step is one of:
    {"output": {...}}            valid JSON (serialised as the model would)
    {"raw": "..."}               arbitrary text (to exercise the repair loop)
    {"refusal": "cyber"}         stop_reason=refusal with a category
    {"truncated": true}          stop_reason=max_tokens with cut-off JSON
    {"error": "transient" | "rate_limited" | "permanent"}
    {"transform": "echo_prompt"} return the request's own <current_prompt> JSON, marked revised
                                 (a deterministic stand-in for the rewrite model; the motion
                                 prompt changes, the keyframe prompt stays byte-identical)
    {"transform": "scene_prompt"} build prompts from the request's <scene> block, so a changed
                                 scene spec gives a changed prompt (a stand-in for re-prompting
                                 after an edit; replaying a fixed prompt would hit the cache)

Packaged fixtures live in ``providers/llm/fixtures/<scenario>/<template>.json`` and are validated
against the node output schemas by ``tests/unit/test_scripted_fixtures.py``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from adsmith.agent.recovery.errors import (
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from adsmith.domain.llm import TokenUsage
from adsmith.providers.llm.base import (
    AssistantMessage,
    LLMRequest,
    LLMResponse,
    TextPart,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_CHARS_PER_TOKEN = 4

Step = dict[str, Any]


def _estimate_input_tokens(req: LLMRequest) -> int:
    chars = len(req.system)
    for msg in req.messages:
        if isinstance(msg, AssistantMessage):
            chars += len(msg.text)
        else:
            chars += sum(len(p.text) for p in msg.parts if isinstance(p, TextPart))
    return max(1, chars // _CHARS_PER_TOKEN)


class ScriptedLLMProvider:
    name = "scripted"

    def __init__(self, scripts: dict[str, list[Step]]) -> None:
        if any(not steps for steps in scripts.values()):
            raise ValueError("every scripted template needs at least one step")
        self._scripts = scripts
        self._calls: defaultdict[str, int] = defaultdict(int)
        self.requests: list[LLMRequest] = []  # inspected by tests

    @classmethod
    def from_scenario(cls, scenario: str, fixtures_dir: Path = FIXTURES_DIR) -> ScriptedLLMProvider:
        directory = fixtures_dir / scenario
        if not directory.is_dir():
            raise FileNotFoundError(f"no scripted scenario {scenario!r} in {fixtures_dir}")
        scripts = {
            p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(directory.glob("*.json"))
        }
        return cls(scripts)

    def override(self, template_name: str, steps: list[Step]) -> None:
        """Replace (or add) one template's script, e.g. critic verdicts in a test."""
        if not steps:
            raise ValueError("a script needs at least one step")
        self._scripts[template_name] = steps
        self._calls[template_name] = 0

    def _pick(self, name: str, steps: list[Step], request: LLMRequest) -> Step:
        """Steps with a ``"when"`` substring are chosen by matching the request text (first match
        wins), so one scripted scenario can answer different edits in any order. Otherwise, and
        when nothing matches, steps are consumed in order (the last one repeats)."""
        keyed = [s for s in steps if "when" in s or "when_all" in s]
        if keyed:
            # Match against the request's own USER section only (the last text part of the first
            # user turn): the cached CONTEXT holds the whole storyboard, which would make every
            # scene match the first scene's markers.
            first = request.messages[0]
            texts = [p.text for p in getattr(first, "parts", ()) if isinstance(p, TextPart)]
            text = texts[-1] if texts else ""
            for s in keyed:
                needles = s.get("when_all") or [s["when"]]
                if all(n in text for n in needles):
                    return s
            plain = [s for s in steps if "when" not in s and "when_all" not in s] or keyed
            return plain[min(self._calls[name], len(plain) - 1)]
        return steps[min(self._calls[name], len(steps) - 1)]

    def calls_for(self, template_name: str) -> int:
        return self._calls[template_name]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        name = request.template.split("@", 1)[0]
        steps = self._scripts.get(name)
        if steps is None:
            raise PermanentProviderError(f"scripted provider has no script for {name!r}")
        step = self._pick(name, steps, request)
        self._calls[name] += 1

        if "error" in step:
            kind = step["error"]
            if kind == "transient":
                raise TransientProviderError("scripted transient error")
            if kind == "rate_limited":
                raise RateLimitedError("scripted 429", retry_after_s=step.get("retry_after_s"))
            raise PermanentProviderError("scripted permanent error")

        text: str | None
        stop = "end_turn"
        category = None
        if step.get("transform") == "echo_prompt":
            text = _echo_prompt(request)
        elif step.get("transform") == "scene_prompt":
            text = _scene_prompt(request)
        elif "output" in step:
            text = json.dumps(step["output"], ensure_ascii=False)
        elif "raw" in step:
            text = step["raw"]
        elif "refusal" in step:
            text, stop, category = None, "refusal", step["refusal"]
        elif step.get("truncated"):
            text, stop = '{"concepts": [{"title": "cut off', "max_tokens"
        else:
            raise ValueError(f"unknown scripted step: {step}")

        usage = TokenUsage(
            input_tokens=_estimate_input_tokens(request),
            output_tokens=max(1, len(text or "") // _CHARS_PER_TOKEN),
        )
        return LLMResponse(
            text=text,
            stop_reason=stop,  # type: ignore[arg-type]
            raw_stop_reason=stop,
            usage=usage,
            served_model=request.model,
            latency_ms=0,
            refusal_category=category,
            native_assistant=None,
            extras={"simulated": True},
        )


def _echo_prompt(request: LLMRequest) -> str:
    # Search the whole conversation: during a repair the last turn is the repair message.
    texts = [
        p.text for m in request.messages for p in getattr(m, "parts", ()) if isinstance(p, TextPart)
    ]
    body = "\n".join(texts)
    start, end = body.find("<current_prompt>"), body.find("</current_prompt>")
    if start < 0 or end < 0:
        raise PermanentProviderError("echo_prompt step needs a <current_prompt> block")
    current = json.loads(body[start + len("<current_prompt>") : end])
    current["motion_prompt"] = f"{current['motion_prompt']} (revised: slower, steadier motion)"
    return json.dumps(current, ensure_ascii=False)


def _scene_prompt(request: LLMRequest) -> str:
    texts = [
        p.text for m in request.messages for p in getattr(m, "parts", ()) if isinstance(p, TextPart)
    ]
    body = "\n".join(texts)
    start, end = body.rfind("<scene>"), body.rfind("</scene>")
    if start < 0 or end < 0:
        raise PermanentProviderError("scene_prompt step needs a <scene> block")
    scene = " ".join(body[start + len("<scene>") : end].split())
    b0, b1 = body.rfind("<scene_bible>"), body.rfind("</scene_bible>")
    bible = " ".join(body[b0 + len("<scene_bible>") : b1].split()) if b0 >= 0 < b1 else ""
    action = next(
        (seg.split(":", 1)[1].strip() for seg in scene.split("  ") if seg.startswith("Action:")),
        scene[:200],
    )
    return json.dumps(
        {
            "keyframe_prompt": f"Vertical 9:16 keyframe. {scene} {bible}"[:2400],
            "motion_prompt": f"Motion as specified for this shot: {action}"[:580],
            "negative_prompt": "text, letters, watermark",
        },
        ensure_ascii=False,
    )
