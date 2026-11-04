# ADR-0007: Model selection per node (profiles)

- Status: Accepted (2026-11-04)

## Context
LLM nodes have very different needs. `understand` and `edit_parser` are narrow extraction tasks on
the interactive path (latency matters). `ideate` and `storyboard` are the creative and planning
core (quality matters). `prompt_writer` and `qa_critic` run once per scene or attempt (volume makes
cost matter). One model everywhere either overpays or underperforms.

## Decision
Model and `effort` are chosen **per node** from named profiles in `config/models.toml`:
`fast`, `balanced` (default), `quality`. Any node can be overridden by env
(`ADSMITH_MODEL__<NODE>=…`). Prices are in the same file, so the budget guard and this ADR use the
same numbers.

| Node | balanced (default) | Why |
|---|---|---|
| understand | `claude-haiku-5-5` / medium | Narrow extraction + dialect normalisation; first step, latency-sensitive; $0.10/$0.50 per Mtok |
| edit_parser | `claude-haiku-5-5` / medium | Interactive; narrow schema; deterministic ref resolution backs it up |
| ideate | `claude-sonnet-5-5` / high | Creative breadth; distinctness validator + repair guard quality |
| storyboard | `claude-sonnet-5-5` / high | Constrained planning (durations, producibility) |
| select, continuity, conflict_check, policy_rewrite | `claude-sonnet-5-5` / medium | Moderate reasoning, low volume |
| prompt_writer | `claude-sonnet-5-5` / medium | N calls per run; quality of prompts drives video quality |
| qa_critic | `claude-sonnet-5-5` / medium | Vision; one call per clip attempt |

`quality` switches `ideate`, `storyboard` and `qa_critic` to `claude-opus-5-5` ($4/$20 per Mtok),
where extra reasoning shows most. `fast` moves extraction-like nodes to Haiku at low effort.

Rough cost for one balanced 4-scene run: about 10–14 LLM calls, roughly $0.05–0.15 of LLM spend. That
is small next to video (~$1.70), so model choice here is mostly about **latency and quality**, not
cost.

## Alternatives considered
- One model everywhere (simple; Opus everywhere overpays and is slower on the interactive path,
  Haiku everywhere weakens ideation/storyboarding).
- Dynamic routing by the LLM: nondeterministic and hard to reason about.

## Consequences
- + Clear cost/latency/quality knob per environment; reproducible via profile name in run config.
- − More configuration surface. `ModelCatalog` validation ensures every profile covers every node and
  every model has a price.
- Model IDs were taken from the current Claude docs, not memory, and are confirmed via the Models
  API in the live test.
