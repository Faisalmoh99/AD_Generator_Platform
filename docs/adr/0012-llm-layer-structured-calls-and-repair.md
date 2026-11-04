# ADR-0012: LLM layer: structured calls, same-conversation repair, caching, cost accounting

- Status: Accepted (2026-11-04)

## Context
Every LLM node must return a validated typed object or a typed error, never crash. Failures must be
repairable and every call (including failed ones) must be costed. The implementation was verified
against the **installed** SDKs (anthropic 2.4.0, built on `httpx2`; openai 3.44), not docs alone.

## Decisions

### 1. `messages.create` + our own validation, not `messages.parse`
Inspecting `anthropic/lib/_parse/_response.py` in 2.4.0 shows `parse()` validates **eagerly
inside the SDK** (`TypeAdapter.validate_json`) and raises on failure. That discards (a) the raw text
the repair turn needs and (b) `usage`, while the failed call is still billed. So providers
call `beta.messages.create(output_config={"effort": …, "format": {"type": "json_schema", "schema":
…}})` and return raw text + usage; `StructuredCaller` validates. The schema comes from the public
`anthropic.transform_schema`, which **moves constraints the API doesn't enforce (min/max length,
ranges, item counts) into descriptions**. Pydantic enforces them on our side, which makes the
repair loop necessary, not decorative. A `convert` step (e.g. `Storyboard.from_draft`) lets domain
invariants that JSON Schema can't express (duration sum, concept distinctness) trigger repair too.

### 2. Repair is appended to the same conversation
On invalid output we append `assistant: <the model's own output, native blocks replayed unchanged>`
then `user: <exact validation errors, "keep what was correct">` and call again (max 2 repairs).
- **Better repairs:** the model edits its own answer instead of regenerating from scratch, so
  the two good concepts stay put and only the duplicate changes.
- **Cheaper:** the original prompt is a prefix of the repair request, so the cached system/context
  blocks are read at 0.1×.
- **API-correct:** history is append-only, and thinking blocks (with signatures) are replayed
  byte-identical. Current models reject or invalidate *edited* history (preserved thinking), and
  prefill is not allowed. Our turn always ends on a user message.
A fresh call with the errors pasted into a new prompt was rejected: it loses the model's draft,
re-pays the full prompt, and tends to produce a different (also possibly invalid) answer.
Truncation (`max_tokens`) is *not* a repair: we retry once with double the budget and the same
messages, since the output was cut off, not wrong.

### 3. Prompt caching
Two explicit breakpoints (`cache_control: ephemeral`, 5-minute TTL): the **system** block (static
per template, and the template loader *rejects* variables in SYSTEM so the prefix is byte-identical
by construction) and an optional **CONTEXT** block (per-run shared context such as the bible and
storyboard). Minimum cacheable prefix on Sonnet/Haiku/Opus 5.5 is 512 tokens. Shorter prefixes
silently don't cache, and no write premium is paid.
Expected savings (Sonnet 5.5: input $2.00, write $2.50, read $0.20 per Mtok): `prompt_writer` with a
~2,000-token shared prefix over 4 scenes + ~2 QA revisions = 6 calls → uncached $0.024, cached
$0.005 write + 5 × $0.0004 reads ≈ $0.007, so **~70% off that prefix**. Similar for `qa_critic`
and repair turns. In dollars this is cents per run (LLM spend is ~$0.05–0.15 vs ~$1.70 of video),
so the bigger win is **latency** (time-to-first-token on repeated prefixes) and rate-limit
headroom (cache reads don't count toward input-token limits on most models). Verified live by
`tests/integration/test_real_apis.py` (second identical prefix must show `cache_read_input_tokens >
0`). Togglable via `ADSMITH_LLM_PROMPT_CACHE`.

### 4. Refusals: server-side fallback first, our rewrite second
For models whose catalog entry has `server_fallback = true` (Sonnet/Opus 5.5), requests carry
`fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), so a classifier false positive is
retried on Anthropic's recommended model for that category, server-side. `response.model` reveals
the model that answered. We record it, price the call by it, and show "answered by fallback X" in
the trace. Haiku 5.5 has no server-side fallback. A refusal that still comes back becomes
`ContentPolicyError` → the policy-rewrite strategy (M5). Disable with
`ADSMITH_ANTHROPIC_SERVER_FALLBACK=false`.

### 5. Cost accounting
Each attempt (initial, repair, truncation retry, failed transient try) yields an `LLMCallRecord`
with `input / cache_write / cache_read / output` tokens. Cost = Σ bucket × catalog price
(`config/models.toml`); thinking tokens are included in output tokens, as billed. An unknown served
model is priced at the most expensive rate per bucket and flagged. Errors carry the records spent
before them. Nodes return the records; the orchestrator books them with
`BudgetLedger.record_actual` (idempotent per record id) and emits each as an `llm_call` /
`llm_repair` event, all in the same atomic state update (ADR-0011).

### 6. Transport retries
SDK retries are disabled (`max_retries=0`). `retry_transient` (full-jitter backoff, separate
bounded budgets for transient and rate-limited, `retry-after` honoured but capped at 60 s, injected
sleep/RNG) owns them, so every retry is visible in the trace and deterministic in tests.

## Consequences
- + One code path for every provider. Validation, repair and accounting are provider-agnostic and
  tested with the scripted provider. The adapter is tested against the SDK's own types.
- + Scripted fixtures are validated against the schemas and run through the real domain conversions
  in CI, so they cannot drift.
- − We depend on `anthropic.transform_schema` semantics. A unit test pins `additionalProperties:
  false`, and a test asserts the SDK major version, forcing re-verification on upgrade.
- − Live behaviour (actual cache hits, fallbacks) is only verified with `--live` and a key.
