"""The Value Scout: an AI fundamentals-vs-price scan for the watchlist.

The idea: for each name, look at the latest quarter's fundamentals (revenue/EPS
vs. expectations, guidance) and the current price / analyst fair value, and judge
whether the price is JUSTIFIED by the fundamentals (already correlated -> no edge)
or DISCONNECTED. When the price sits BELOW what the last quarter's fundamentals
justify, that's a value gap to the upside - a buy candidate. When the price
already reflects the news, skip it.

Fundamentals move quarterly, not every 30 minutes, so the scan is cached to
docs/data/fundamentals.json and only refreshed when older than the configured
TTL. That keeps it ~free per cycle while still feeding the trader a fresh
value signal.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import anthropic

from .config import Config
from .costs import price_usage

FUNDAMENTALS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "docs", "data", "fundamentals.json"
)

SCOUT_SYSTEM = """You are a disciplined value analyst. For each ticker you are \
given, use web search to establish two things: (1) the most recent quarterly \
results and guidance (revenue and EPS vs. expectations, growth, forward guide), \
and (2) the current share price versus a reasonable fair value (analyst targets, \
simple valuation vs. peers/history).

Then judge the KEY question: is the current price justified by - i.e. does it \
already reflect - the latest fundamentals, or is it disconnected?
- verdict "cheap" + undervalued=true ONLY when the price appears to sit BELOW \
what the latest quarter's fundamentals and guidance justify (a value gap to the \
upside worth considering).
- verdict "fair" when price broadly reflects the fundamentals (already \
correlated - no edge; do not chase).
- verdict "rich" when price runs well ahead of what fundamentals support \
(overvalued - avoid / candidate to trim).

For ETFs or names where company earnings don't apply, use verdict "fair" and \
undervalued=false with a one-line note. Keep each note to one crisp sentence \
citing the fundamental-vs-price gap. Be decisive and honest; most names are \
"fair"."""

SCOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["cheap", "fair", "rich"]},
                    "undervalued": {"type": "boolean"},
                    "note": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["symbol", "verdict", "undervalued", "note", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["signals"],
    "additionalProperties": False,
}


def _load_cache() -> dict | None:
    try:
        with open(FUNDAMENTALS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _age_hours(generated_at: str | None) -> float:
    if not generated_at:
        return 1e9
    try:
        then = datetime.fromisoformat(generated_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0
    except ValueError:
        return 1e9


class ValueScout:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.last_cost = 0.0

    def get_scan(self, symbols: list[str]) -> dict:
        """Return the cached value scan, refreshing it via one AI call only when
        the cache is missing or older than the TTL. Returns
        {generated_at, model, signals:[...]}."""
        self.last_cost = 0.0
        cache = _load_cache()
        if cache and _age_hours(cache.get("generated_at")) < self.cfg.fundamentals_ttl_hours:
            return cache
        try:
            scan = self._run_scan(symbols)
        except Exception as e:  # keep the stale cache (or empty) on failure
            if cache:
                cache["stale_error"] = str(e)[:200]
                return cache
            return {"generated_at": None, "model": self.cfg.model, "signals": [],
                    "error": str(e)[:200]}
        os.makedirs(os.path.dirname(FUNDAMENTALS_PATH), exist_ok=True)
        with open(FUNDAMENTALS_PATH, "w") as f:
            json.dump(scan, f, indent=2)
        return scan

    def _run_scan(self, symbols: list[str]) -> dict:
        tools = []
        if self.cfg.enable_web_search and "haiku" not in self.cfg.model.lower():
            tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}]
        user = (
            "Run your fundamentals-vs-price value scan on these tickers and return "
            "the JSON signals:\n" + ", ".join(symbols)
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for _ in range(6):  # allow web-search pause_turn continuation
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=8000,
                system=SCOUT_SYSTEM,
                tools=tools,
                messages=messages,
                output_config={"format": {"type": "json_schema", "schema": SCOUT_SCHEMA}},
            )
            self.last_cost += price_usage(self.cfg.model, resp.usage)
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"signals": []}
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": self.cfg.model,
            "signals": data.get("signals", []),
        }

    @staticmethod
    def summarize(scan: dict) -> str:
        """A compact text block for the trader's research context."""
        signals = scan.get("signals") or []
        if not signals:
            return "FUNDAMENTAL VALUE SCAN: (unavailable this cycle)"
        cheap = [s for s in signals if s.get("undervalued")]
        rich = [s for s in signals if s.get("verdict") == "rich"]
        lines = [
            f"FUNDAMENTAL VALUE SCAN (as of {scan.get('generated_at', 'n/a')[:16]}):",
        ]
        if cheap:
            lines.append("  Undervalued (price below fundamentals - BUY candidates):")
            for s in cheap:
                lines.append(f"    {s['symbol']}: {s.get('note', '').strip()}")
        if rich:
            lines.append("  Rich (price ahead of fundamentals - avoid / trim):")
            for s in rich:
                lines.append(f"    {s['symbol']}: {s.get('note', '').strip()}")
        if not cheap and not rich:
            lines.append("  No clear value gaps this scan; most names fairly priced.")
        return "\n".join(lines)
