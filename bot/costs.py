"""AI operating-cost accounting.

The bot pays real Anthropic API costs every cycle (research + structure +
audit calls). This module prices that usage so the engine can track whether
the strategy is actually beating its own running costs - the honest answer to
"is this AI making enough to cover itself?".
"""
from __future__ import annotations

# US$ per 1M tokens: (input, output). Cache reads bill ~0.1x input, cache
# writes ~1.25x input. Keep this in sync with current Anthropic pricing.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT = (2.0, 10.0)  # assume Sonnet-class if the model is unknown

WEB_SEARCH_COST = 0.01  # ~$10 per 1000 searches


def price_usage(model: str, usage) -> float:
    """Return the US$ cost of one API response's token usage (best-effort)."""
    inp_price, out_price = MODEL_PRICING.get(model, _DEFAULT)

    def g(attr: str) -> int:
        return int(getattr(usage, attr, 0) or 0)

    cost = (
        g("input_tokens") / 1e6 * inp_price
        + g("output_tokens") / 1e6 * out_price
        + g("cache_read_input_tokens") / 1e6 * inp_price * 0.1
        + g("cache_creation_input_tokens") / 1e6 * inp_price * 1.25
    )
    # Server-side web search, if the SDK reports it.
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        cost += int(getattr(stu, "web_search_requests", 0) or 0) * WEB_SEARCH_COST
    return cost
