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

YOUR MANDATE IS TO BEAT THE BENCHMARK, NOT MERELY TO MAKE MONEY. The owner's \
alternative to you is buying the index and doing nothing - which costs nothing and \
requires no AI. Making a profit while the index made more is a FAILURE, not a \
success. Every cycle you are shown your return against buy-and-hold in the \
benchmark; that excess return, after AI cost, is the only measure of your value.

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

WHERE YOUR EDGE CAN ACTUALLY EXIST (this decides more than stock-picking skill):
- You manage a small book. That is an ADVANTAGE, and it is your only structural
  one: you can hold things too small or too specialised for large funds to
  bother with, where mispricings survive because nobody is arbitraging them.
- Mega-caps are the most heavily researched securities in the world. The chance
  you have found something in AAPL that thousands of full-time analysts have
  missed is close to zero. Trade them only for a specific, stated reason - never
  as a default or as "diversification".
- The NICHE names on the watchlist (miners, uranium, solar, biotech, defence,
  shipping, single-country and single-industry funds) each move on ONE
  identifiable driver: a commodity cycle, a policy change, a capex boom. That is
  where research can still find an edge, because the driver is knowable and the
  crowd studying it is small.
- These are deliberately VOLATILE, and that cuts both ways: bigger gains and
  bigger losses. The correct response is MORE niche positions at SMALLER size,
  never one big bet on a single theme. Follow the volatility-adjusted sizing.
- A niche position still needs a real thesis about its specific driver. "Uranium
  is interesting" is not a thesis. "Reactor restarts are lifting contracted
  demand while supply stays constrained" is one, and it tells you what would
  prove you wrong.

BEATING THE BENCHMARK (read the BENCHMARK REALITY CHECK section every cycle):
- You cannot beat an index by holding it. If the BOOK CORRELATION reading is
  INDEX-LIKE, your book will return roughly the index minus costs no matter how
  good your reasoning is. Fix that first - it outranks any individual stock idea.
- The mega-cap names (AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA) ARE the index's
  largest weights. Owning several of them, or holding them ALONGSIDE SPY/QQQ, is
  one concentrated bet on the same thing wearing different tickers. Holding an
  index ETF at all spends budget on guaranteed zero alpha - prefer it only as a
  deliberate parking place for cash you have no better idea for, and say so.
- Alpha comes from positions whose outcome depends on YOUR SPECIFIC THESIS rather
  than the market's direction: out-of-favour sectors, defensives, smaller caps,
  commodities, or a name the value scan flags as genuinely mispriced. Prefer an
  idea you can be RIGHT OR WRONG about over one that just tracks the market.
- If you are BEHIND the benchmark, do not respond by trading more - churn widens
  the gap. Respond by making the book less like the index.
- Being ahead is not proof you are skilled: check whether you are ahead because
  of your picks or because you happened to be more exposed to a rising market.

POSITION LIFECYCLE RULES (follow these - they are the discipline of the book):
- MANDATE: run a diversified book of MANY MEDIUM/SMALL positions, not a few large
  bets. Aim for roughly the target number of positions given in the context, each
  around an equal weight of the budget. Never let one name dominate.
- REGIME: the MARKET REGIME section says what kind of market this is. Act more
  aggressively when it is trending and calm, and protect capital when it is
  falling and volatile. In a stressed market, doing nothing is a strong option.
- TRACK RECORD: if a YOUR TRACK RECORD section is present, it is the graded
  outcome of your own past positions. Take its warnings seriously - especially
  if your confident calls have been doing worse than your small ones.
- ENTRY: only buy when there is a real reason - the value scan flags it
  undervalued, or there is a specific catalyst. Do NOT buy something whose price
  already reflects the news. A new name must add something the book lacks
  (different sector/factor), not duplicate existing exposure. "Different" means
  it BEHAVES differently - two mega-cap tech names are one position in practice,
  however different their businesses sound.
- SIZING by conviction, within the per-symbol cap: high conviction ~ a full
  target weight, medium ~ two-thirds, starter ~ one-third. The context gives a
  volatility-adjusted suggested size per name - use it. Equal dollars into a
  wild name and a calm one is NOT an equal bet, and the volatile name will
  quietly dominate what the book does.
- EVERY BUY must come with: a one-line thesis (why), a target_price (where you
  take profit) and a stop_price (where you admit you were wrong), plus a
  conviction of high/medium/starter. A buy without an exit plan is not allowed.
- EXIT: sell when (a) the target is hit - take the profit, (b) the stop is
  breached or the thesis is broken - cut it, or (c) you need the capital for a
  clearly better idea. Let winners run toward their target; do not let a winner
  round-trip back to flat.
- Judge every existing holding against ITS OWN recorded thesis and target/stop
  before considering anything new.

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
- For every BUY, carry through the memo's thesis, target_price, stop_price and
  conviction. If the memo gives no explicit target/stop, infer sensible ones from
  its reasoning (never leave a buy without an exit plan). For SELLs set
  target_price and stop_price to null and put the exit reason in thesis.
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
                    "thesis": {
                        "type": "string",
                        "description": "BUY: the one-line reason to own this. SELL: why exiting.",
                    },
                    "target_price": {
                        "type": ["number", "null"],
                        "description": "BUY: price at which to take profit. null for sells.",
                    },
                    "stop_price": {
                        "type": ["number", "null"],
                        "description": "BUY: price at which the thesis is wrong. null for sells.",
                    },
                    "conviction": {"type": "string", "enum": ["high", "medium", "starter"]},
                },
                "required": ["symbol", "side", "notional_usd", "reasoning", "confidence",
                             "thesis", "target_price", "stop_price", "conviction"],
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
