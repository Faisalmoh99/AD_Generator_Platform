"""Token usage → USD, from the catalog in ``config/models.toml``."""

from __future__ import annotations

from adsmith.config import LLMModelSpec, ModelCatalog
from adsmith.domain.llm import TokenUsage

_PER_MILLION = 1_000_000


def llm_cost(usage: TokenUsage, spec: LLMModelSpec) -> float:
    return (
        usage.input_tokens * spec.input
        + usage.cache_write_tokens * spec.cache_write
        + usage.cache_read_tokens * spec.cache_read
        + usage.output_tokens * spec.output
    ) / _PER_MILLION


def price_llm_call(
    catalog: ModelCatalog, served_model: str, usage: TokenUsage
) -> tuple[float, bool]:
    """Price by the model that actually answered (a server-side fallback may differ from the one
    requested). Unknown model → charge the most expensive rate per bucket and flag it, so the budget
    guard errs on the safe side."""
    spec = catalog.llm_models.get(served_model)
    if spec is not None:
        return llm_cost(usage, spec), False
    specs = list(catalog.llm_models.values())
    worst = LLMModelSpec(
        input=max(s.input for s in specs),
        output=max(s.output for s in specs),
        cache_write=max(s.cache_write for s in specs),
        cache_read=max(s.cache_read for s in specs),
    )
    return llm_cost(usage, worst), True
