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
| understand | `claude-haiku-5-5` / **low** | Narrow extraction + dialect normalisation; first step, latency-sensitive; $0.10/$0.50 per Mtok. Measured: low 3.5 s vs medium 5.7 s, same quality (below) |
| edit_parser | `claude-haiku-5-5` / medium | Interactive; narrow schema; deterministic ref resolution backs it up |
| ideate | `claude-sonnet-5-5` / **high** | Creative breadth. Measured: medium/low are 2.3× faster but regress to category clichés (below) |
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


## Measurements (2026-11-04, real API, Arabic perfume request, balanced prompts)

Bench script calls the provider directly with the real rendered prompts and schemas; effort is the
only variable. n=3 for understand and n=2 for ideate, so medians are indicative, not statistically
tight.

| Node / model | Effort | Median latency (min–max) | Output tokens | Cost / call |
|---|---|---|---|---|
| understand / Haiku 5.5 | medium | 5.7 s (3.9–7.8) | ~935 | $0.0006 |
| understand / Haiku 5.5 | **low** | **3.5 s** (3.5–3.5) | ~480 | $0.0003 |
| ideate / Sonnet 5.5 | **high** | **49.6 s** (47.1–52.1) | ~5,380 | $0.058 |
| ideate / Sonnet 5.5 | medium | 21.2 s (20.8–21.5) | ~2,140 | $0.023 |
| ideate / Sonnet 5.5 | low | 21.2 s (20.8–21.7) | ~2,150 | $0.023 |

**understand → low.** Both levels produce the same language, dialect, product, audience, duration
and constraint fields. Low is a little terser (tone has 2 values instead of 3, and the alcohol note was
dropped). One slip at low: an assumption cites "أبي" as a dialect cue although it is not in the
request. Assumptions are shown to the user and not consumed downstream, so this is acceptable.
(An earlier single 19.7 s call was a cold-start outlier.)

**ideate stays at high.** Low gives no speed-up over medium (Sonnet already thinks briefly at
medium), and both lower levels lose originality. Medium and low each produced a "visible trail of
light behind him" concept (the scent-trail cliché), and medium brought back the falcon/desert
gathering. At high, the model's own cliché list named "dunes with a falcon or incense smoke"
and its concepts avoided all of them: *the room remembers him* (curtains and pages keep moving after he has
left), *the haughty cat that only follows him*, *Najd light crossing the bottle from dawn to night*.
Ideation is the one place where the extra ~28 s buys the output the product exists for. The
demo mitigates the wait with a live stage indicator and streams the trace. The `fast` profile runs
ideate at low for users who prefer speed.
