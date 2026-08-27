"""The AI brain: Claude researches, then emits a structured trade plan.

Two phases keep each Claude feature in its lane:

  1. ANALYST - Claude gets the live portfolio + market data and (optionally)
     the web-search tool, thinks it through, and writes a research memo with
     a recommendation. This is where the "AI research" happens.

  2. STRUCTURER - a second call turns that memo into an exact JSON trade plan
     (a list of intended orders) using a strict output schema, so the engine
     always gets machine-readable orders it can risk-check and execute.

The engine - not Claude - enforces the hard risk limits afterwards, so a
bad decision can never exceed the configured guardrails.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from .config import Config
from .costs import price_usage

ANALYST_SYSTEM = """You are a disciplined AI equity analyst and portfolio manager \
running a systematic trading account. You are given a live snapshot of the account \
(cash, equity, open positions and their P&L) and recent market data for a watchlist \
of tickers. Your job each cycle is to decide what, if anything, to trade.

How to work:
- Study the account first. Respect existing positions; consider trimming losers, \
taking profits, and rebalancing, not only buying new names.
- Use the recent price action provided. If the web_search tool is available, use it \
to check current news, earnings, and sentiment for the most relevant tickers before \
deciding. Cite what you found in your memo.
- Think in terms of risk-adjusted decisions. It is completely acceptable - often \
correct - to do nothing on a given cycle. Do not trade for the sake of trading.
- This account pays a real AI operating cost every cycle. Treat that as a hurdle: \
only act when the expected edge clearly exceeds trading and operating costs, and \
aim to grow the book NET of those costs. Churn is how a small account bleeds - \
holding is free, trading is not.
- You may only BUY names on the watchlist, and only SELL names you already hold \
(no shorting). Size positions sensibly relative to total equity.
- A FUNDAMENTAL VALUE SCAN may be provided: names flagged undervalued (price below \
what last quarter's fundamentals justify) are your best BUY candidates; names \
whose price already reflects the fundamentals are not edges - don't chase them; \
names flagged rich are candidates to avoid or trim.
- If a STOP-LOSS REVIEW section lists losing positions, address each explicitly: \
cut it (add a SELL) if the loss reflects a broken thesis or clear downtrend, or \
state a specific reason to hold. Never ignore a flagged loser.

Output a concise research memo: the current market read, then for each ticker you \
want to act on, a one-line thesis and the rough dollar size. End with a clear \
recommendation. Do NOT output JSON here - just your written analysis."""

STRUCTURER_SYSTEM = """You convert an equity analyst's research memo into a precise \
trade plan. Read the memo and the account snapshot, then output the intended orders \
that faithfully implement the memo's recommendation.

Rules you must follow:
- Only BUY tickers that are on the provided watchlist.
- Only SELL tickers that appear in the current positions (never short).
- Express each order as a positive US-dollar `notional` amount.
- If the memo recommends no action, return an empty orders list.
- `confidence` is your 0-1 confidence in that specific order.
Do not invent trades the memo does not support."""

# JSON schema the structurer must produce. `strict` + additionalProperties:false
# guarantees the engine can json.loads the first text block safely.
TRADE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "market_summary": {
            "type": "string",
            "description": "One or two sentences summarising the current market read.",
        },
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "notional_usd": {"type": "number"},
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["symbol", "side", "notional_usd", "reasoning", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_summary", "orders"],
    "additionalProperties": False,
}


class Brain:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # 10-min default timeout is generous enough for a research turn.
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.run_cost = 0.0  # US$ spent by this cycle's research + structure calls

    def _bill(self, resp) -> None:
        self.run_cost += price_usage(self.cfg.model, resp.usage)

    def research(self, context: str) -> str:
        """Phase 1: produce a written research memo (may use web search)."""
        self.run_cost = 0.0  # reset at the start of each cycle
        tools = []
        # Web search runs server-side and needs an Opus/Sonnet-class model; Haiku
        # doesn't support this tool variant, so quietly skip it there.
        if self.cfg.enable_web_search and "haiku" not in self.cfg.model.lower():
            tools = [
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
            ]

        messages: list[dict[str, Any]] = [{"role": "user", "content": context}]

        # Server tools (web_search) run on Anthropic's side; a long turn can come
        # back as `pause_turn`, which we simply resend to let it continue.
        for _ in range(6):
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=ANALYST_SYSTEM,
                tools=tools,
                messages=messages,
            )
            self._bill(resp)
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break

        memo = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        return memo or "(the analyst returned no written memo)"

    def structure(self, memo: str, account_context: str) -> dict:
        """Phase 2: turn the memo into a strict JSON trade plan."""
        user = (
            f"ACCOUNT SNAPSHOT & WATCHLIST\n{account_context}\n\n"
            f"ANALYST MEMO\n{memo}\n\n"
            "Produce the trade plan as JSON."
        )
        resp = self.client.messages.create(
            model=self.cfg.model,
            max_tokens=8000,
            system=STRUCTURER_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": TRADE_PLAN_SCHEMA},
            },
        )
        self._bill(resp)
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        try:
            plan = json.loads(text)
        except json.JSONDecodeError:
            plan = {"market_summary": "Failed to parse trade plan.", "orders": []}
        plan.setdefault("market_summary", "")
        plan.setdefault("orders", [])
        return plan
