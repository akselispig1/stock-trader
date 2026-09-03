"""The self-review: the bot reads its own results and rewrites its own playbook.

This is the recursive part. A dedicated Claude call periodically reads:

  - every position the bot has closed, graded against the plan it was opened with
  - its own recent research memos and decisions
  - whether it is beating the benchmark, and how index-like the book has become
  - the rules it wrote for itself last time, each with the record as it stood
    when the rule was written, and the record now

and proposes changes to that playbook. The loop closes because the reviewer is
judging its OWN previous changes: rules that did not help get retired, and the
quality of the next proposal depends on how honestly it read the last one.

WHAT IT CAN AND CANNOT REACH

It writes trading heuristics into the playbook and nothing else. It cannot
change risk limits, position caps, the cash reserve, the auditor, or any code,
because none of those read the playbook - they read Config, and Config is not
writable from here. A playbook rule is text in a prompt. The worst a bad rule
can do is produce a bad trade proposal, which then meets exactly the same
allocation caps, cash reserve, order limits and independent auditor as any other.

WHY IT WAITS

Rules written on four closed trades would be superstition dressed as strategy.
The review refuses to run below a minimum sample, and refuses again unless
enough new positions have closed since the last one to say anything new.
"""
from __future__ import annotations

import json

import anthropic

from . import playbook, scorecard
from .config import Config
from .costs import price_usage

REVIEW_SYSTEM = """You are reviewing the track record of an automated trading \
system and improving the STRATEGY PLAYBOOK it follows. You are not trading now; \
you are deciding what the trader should do differently.

WHAT YOU ARE LOOKING AT
- Every position the system has closed, graded against the thesis, target and \
stop it was opened with.
- Its own recent decisions and reasoning.
- Whether it beat the benchmark, and how correlated its book was to the index.
- The playbook rules from previous reviews - INCLUDING YOUR OWN - each with the \
record as it stood when the rule was written and the record now.

YOUR JOB
1. Judge your own previous rules first. For each: has the record moved the way \
the rule predicted? If a rule has had a fair number of positions to work with \
and the evidence does not support it, RETIRE IT. Retiring your own rule when it \
failed is the most valuable thing you can do here - a playbook that only grows \
becomes superstition.
2. Look for a PATTERN in the losses, not a story about individual trades. Good \
patterns are structural and testable: an entry condition that keeps failing, an \
exit that fires too early, a kind of name that never works, a market regime the \
system handles badly.
3. Propose at most 2 new rules. Each must be specific enough to follow \
mechanically, and must come with a prediction that a later review can check.

WHAT MAKES A GOOD RULE
- Specific and checkable: "Do not enter a niche sector ETF within 3 days of that \
sector's earnings cluster" - not "be more careful around earnings".
- Grounded in THIS record, citing what actually happened.
- About selection, timing, sizing preference, or exit judgment.

WHAT YOU MUST NOT PROPOSE
- Anything changing risk limits, allocation caps, the cash reserve, order \
limits, the capital cap, the auditor, or any configuration setting. Those are \
enforced in code that never reads this playbook; proposing them is wasted effort \
and will be rejected automatically.
- Anything about live trading, real money, margin, shorting, or credentials.
- Vague encouragement. "Pick better stocks" is not a rule.

HONESTY REQUIREMENTS
- If the sample is too small to support a conclusion, say so and propose \
NOTHING. An empty proposal is a valid and often correct answer.
- Do not invent a pattern to justify acting. Noise in 8 trades looks exactly \
like a pattern.
- If the system is losing to the benchmark, say plainly whether the cause is \
the picks, the sizing, or being too index-like."""

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assessment", "add_rules", "retire_rule_ids", "confidence"],
    "properties": {
        "assessment": {
            "type": "string",
            "description": "Honest read of the record: what is working, what is not, "
                           "and whether there is enough data to say.",
        },
        "add_rules": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rule", "rationale", "hypothesis"],
                "properties": {
                    "rule": {"type": "string", "description": "The heuristic, mechanically followable."},
                    "rationale": {"type": "string", "description": "What in THIS record produced it."},
                    "hypothesis": {"type": "string", "description": "What should improve, checkable later."},
                },
            },
        },
        "retire_rule_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of existing rules the evidence does not support.",
        },
        "confidence": {
            "type": "number",
            "description": "0-1 confidence that these changes are real signal, not noise.",
        },
    },
}


class Reviewer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.last_cost = 0.0

    def due(self, stats: dict, pb: dict) -> tuple[bool, str]:
        """Is there enough NEW evidence to justify a review?"""
        total = stats.get("total", 0)
        if total < self.cfg.evolve_min_trades:
            return False, (f"{total} closed positions; need "
                           f"{self.cfg.evolve_min_trades} before drawing conclusions")
        since = total - int(pb.get("last_review_trades", 0) or 0)
        if pb.get("last_review_trades") and since < self.cfg.evolve_every_trades:
            return False, (f"only {since} positions closed since the last review; "
                           f"need {self.cfg.evolve_every_trades}")
        return True, f"{total} closed positions, {since} since the last review"

    def review(self, stats: dict, pb: dict, journal: str, bench: str) -> dict | None:
        """Ask for playbook changes. Returns the parsed proposal, or None on error."""
        self.last_cost = 0.0
        closed = scorecard.load_closed()
        # Most recent first, capped: a long tail of old trades adds tokens
        # without changing the conclusion.
        recent = list(reversed(closed))[:60]

        rows = []
        for c in recent:
            r = f"{c['return_pct']:+.1f}%" if c.get("return_pct") is not None else "n/a"
            hold = f"{c['hold_days']:.0f}d" if c.get("hold_days") is not None else "?"
            rows.append(
                f"  {c['symbol']:<6} {c.get('conviction','?'):<8} {r:>8} in {hold:<5} "
                f"{c.get('outcome','?'):<14} {str(c.get('thesis',''))[:90]}"
            )

        user = "\n".join([
            "CLOSED POSITIONS (most recent first) - symbol, conviction, return, hold, how it ended, thesis:",
            *rows,
            "",
            scorecard.summarize(stats),
            "",
            bench or "(no benchmark reading available)",
            "",
            journal or "(no recent decisions recorded)",
            "",
            "YOUR CURRENT PLAYBOOK, with the record when each rule was written:",
            playbook.rules_for_review(pb, stats),
            "",
            "Review the record. Retire rules the evidence does not support, and "
            "propose at most 2 new ones. If the sample cannot support a conclusion, "
            "propose nothing and say so.",
        ])

        try:
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                system=REVIEW_SYSTEM,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": REVIEW_SCHEMA},
                },
            )
            self.last_cost = price_usage(self.cfg.model, resp.usage)
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            out = json.loads(text)
        except (anthropic.APIError, json.JSONDecodeError, ValueError) as e:
            # A failed review must never stop trading: the bot simply keeps the
            # playbook it already has.
            print(f"[evolve] review failed, keeping current playbook: {e}")
            return None

        out.setdefault("assessment", "")
        out.setdefault("add_rules", [])
        out.setdefault("retire_rule_ids", [])
        out.setdefault("confidence", 0.0)
        return out
