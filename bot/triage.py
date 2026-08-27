"""Cheap triage gate: decide whether a full (expensive) research cycle is worth it.

Most cycles are quiet - flat book, no fresh news, nothing to deploy - yet a full
run still pays for research + structure + audit. This runs a single small, cheap
model call (Haiku by default) first to decide whether the expensive pipeline
should run at all. On quiet ticks it returns "skip" for ~$0.002 instead of the
~$0.15 a full Sonnet cycle costs.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from .config import Config
from .costs import price_usage

TRIAGE_SYSTEM = """You are a fast, cheap gatekeeper for an AI trading bot. Before \
it spends money on a full research cycle, you decide whether anything has plausibly \
changed enough to be worth it.

Return run_full = true only if there is a real reason to act this cycle, e.g.:
- a holding has moved materially (roughly >=2%) since entry (up = maybe trim, down = maybe cut),
- there is fresh, market-moving news on a holding or watchlist name,
- there is meaningful un-deployed cash to put to work (and it's early / the book isn't already built),
- a clear risk event is unfolding.

Return run_full = false when the book is flat, there's no fresh catalyst, and \
there's nothing actionable - trading isn't free, and skipping a quiet cycle saves \
real money. Bias toward false on genuinely quiet ticks, but never skip if a \
position is down heavily or a large share of the budget is sitting idle with no \
reason. Be decisive and brief."""

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_full": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["run_full", "reason"],
    "additionalProperties": False,
}


class Triage:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.last_cost = 0.0

    def should_run(self, context: str) -> tuple[bool, str]:
        """Return (run_full, reason). Fails open (run the full cycle) on error, so
        a triage hiccup never silently stops the bot from trading."""
        self.last_cost = 0.0
        try:
            resp = self.client.messages.create(
                model=self.cfg.triage_model,
                max_tokens=400,
                system=TRIAGE_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": f"{context}\n\nShould the bot run a full research "
                               f"cycle now? Answer as JSON.",
                }],
                output_config={"format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
            )
            self.last_cost = price_usage(self.cfg.triage_model, resp.usage)
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            data = json.loads(text)
            return bool(data.get("run_full", True)), str(data.get("reason", ""))
        except Exception as e:  # fail open
            return True, f"triage unavailable ({e}); running full cycle"
