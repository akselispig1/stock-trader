"""The Auditor: a second, independent AI that reviews the trader AI's plan.

This is the project's anti-"black box" mechanism. The trader AI (brain.py)
proposes trades; before any of them execute, a separate Claude call with a
deliberately skeptical, owner-protecting mandate checks that each trade is
genuinely justified by the stated research - not hype-chasing, not vague
technical hand-waving, not contradicting the memo's own risk points. It can
veto individual orders or reject the whole cycle, and it writes a plain-English
audit the account owner can read on the dashboard.

It is an *additional* safety layer on top of the hard, code-enforced risk
limits in engine.py - never a replacement for them.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from .config import Config

AUDITOR_SYSTEM = """You are an INDEPENDENT trade auditor. You did not make these \
trades - a separate AI portfolio manager did, and your job is to protect the \
account owner by checking the manager's work with a skeptical eye. You are the \
reason this system is not a "black box": every trade must be transparently \
justified, or you flag it.

For the proposed orders, judge each one against the manager's own research memo:
- Is this specific trade actually SUPPORTED by the reasoning in the memo, or does \
it appear from nowhere (a black-box order)?
- Is the justification substantive, or vague hype / generic "momentum looks good" \
hand-waving with no real thesis?
- Does it CONTRADICT risks the memo itself raised (e.g. buying a name the memo \
called overvalued or said to avoid)?
- Is the book over-concentrated or chasing a crowded/into-a-catalyst trade?

Decision rules:
- `veto` an order that is not clearly and honestly justified. When in doubt, veto \
- protecting capital beats permitting a weakly-argued trade.
- `approve` an order whose thesis is clear, specific and consistent with the memo.
- Overall `verdict`: "approve" (all good), "flag" (proceeds but with concerns worth \
the owner's attention), or "reject" (systemic problem - veto the whole cycle).
- `transparency_score` (0-1): how well-reasoned and transparent the decisions are \
overall. Low means it reads like an opaque or hype-driven black box.

Write the `summary` in plain English, addressed to the account owner, as if you \
are the watchdog they hired. Be direct."""

AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "flag", "reject"]},
        "transparency_score": {"type": "number"},
        "summary": {"type": "string"},
        "order_reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "decision": {"type": "string", "enum": ["approve", "veto"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "decision", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "transparency_score", "summary", "order_reviews"],
    "additionalProperties": False,
}


class Auditor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def audit(self, memo: str, context: str, orders: list[dict]) -> dict:
        """Review the orders about to be placed. `orders` is the post-risk-check
        list of approved orders; index in that list is the review key."""
        numbered = "\n".join(
            f"  [{i}] {o['side'].upper()} {o['symbol']} ${o['notional_usd']:,.0f} "
            f"- manager's stated reason: {o.get('reasoning', '').strip()}"
            for i, o in enumerate(orders)
        )
        user = (
            f"ACCOUNT & BUDGET CONTEXT\n{context}\n\n"
            f"THE MANAGER'S RESEARCH MEMO\n{memo}\n\n"
            f"ORDERS ABOUT TO BE PLACED (review each by index)\n{numbered}\n\n"
            "Audit these. Return your verdict as JSON."
        )
        resp = self.client.messages.create(
            model=self.cfg.model,
            max_tokens=4000,
            system=AUDITOR_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": AUDIT_SCHEMA},
            },
        )
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        try:
            audit = json.loads(text)
        except json.JSONDecodeError:
            audit = {}
        audit.setdefault("verdict", "flag")
        audit.setdefault("transparency_score", None)
        audit.setdefault("summary", "Auditor returned no readable summary.")
        audit.setdefault("order_reviews", [])
        return audit
