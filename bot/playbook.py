"""The playbook: the one thing the bot is allowed to change about itself.

WHAT THIS IS

A versioned list of trading heuristics the bot writes for itself, learned from
its own graded results, and re-read into its prompt every cycle. Each rule
carries the evidence that produced it, a testable prediction, and the record as
it stood when the rule was written - so a later review can ask whether the rule
actually helped and retire it if not.

That last part is what makes the loop recursive: the reviewer reads the outcomes
of its OWN past changes, so the quality of its future proposals depends on how
well it judged the earlier ones.

WHAT THIS IS DELIBERATELY NOT

It cannot change risk limits, and not because a prompt asks it not to. The
limits live in Config and are enforced in engine.risk_check, which never reads
this file. A playbook rule is TEXT IN A PROMPT and nothing else: at worst a bad
rule produces a bad trade proposal, which then hits the same allocation caps,
cash reserve, order limits and independent auditor as every other proposal.

That architectural boundary - advice in the prompt, enforcement in Python - is
the whole safety story, and it is why the validation below can afford to be a
second line of defence rather than the only one. The validator rejects rules
that TRY to reach past it, mostly to catch a confused model early and keep the
playbook readable, not because a rule slipping through would disable a limit.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "data")
PLAYBOOK_PATH = os.path.join(DATA_DIR, "playbook.json")
HISTORY_PATH = os.path.join(DATA_DIR, "playbook_history.jsonl")

# Hard ceiling on how much self-written guidance can accumulate. Without this
# the prompt grows every review until the rules crowd out the market data they
# are supposed to interpret.
MAX_ACTIVE_RULES = 12
MAX_RULE_CHARS = 320

# A rule must not claim authority over the enforcement layer. These are the
# names and phrasings that would indicate the model thinks it can.
FORBIDDEN_PATTERNS = [
    # Config identifiers - a rule naming one is trying to change a setting.
    r"\b(MAX_|MIN_|ALLOW_|ENABLE_|DISABLE_|CAPITAL_CAP|RISK_LEVEL|TRADING_MODE"
    r"|STOP_LOSS_|TARGET_POSITIONS|WATCHLIST|DRY_RUN|LIVE_REQUIRE_APPROVAL)\w*",
    # Attempts to switch off a guard.
    r"\b(disable|bypass|ignore|override|skip|circumvent|turn off|switch off)\b[^.]{0,40}"
    r"\b(auditor|audit|risk|limit|cap|check|guard|stop|reserve|constraint)\b",
    # Prompt-injection shapes.
    r"\bignore\b[^.]{0,30}\b(previous|prior|above|earlier)\b[^.]{0,20}\b(instruction|rule|prompt)",
    r"\byou are (now|no longer)\b",
    # Anything reaching for real money or leverage.
    r"\b(live trading|real money|go live|margin|leverage|short sell|shorting)\b",
    # Anything reaching outside the trading decision.
    # `\.env` and `os\.environ` are matched without a leading \b: a word
    # boundary cannot precede a dot, so \b\.env would never match at all.
    r"\b(api key|secret|credential)\b|\.env\b|\bos\.environ|\b(subprocess|exec|eval)\b",
]
_FORBIDDEN = [re.compile(p, re.I) for p in FORBIDDEN_PATTERNS]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_rule(text: str) -> tuple[bool, str]:
    """Second line of defence: is this a trading heuristic, or an escape attempt?

    Returns (ok, reason). A rejection is worth surfacing rather than silently
    dropping - a model repeatedly trying to reach past the boundary is
    information about the model, not noise.
    """
    t = (text or "").strip()
    if not t:
        return False, "empty rule"
    if len(t) > MAX_RULE_CHARS:
        return False, f"too long ({len(t)} > {MAX_RULE_CHARS} chars)"
    for pat in _FORBIDDEN:
        m = pat.search(t)
        if m:
            return False, f"tries to reach past the strategy layer: {m.group(0)!r}"
    return True, ""


def load() -> dict:
    try:
        with open(PLAYBOOK_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"version": 0, "updated_at": None, "rules": []}


def save(pb: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PLAYBOOK_PATH, "w") as f:
        json.dump(pb, f, indent=2)


def _archive(pb: dict, note: str) -> None:
    """Append the pre-change state, so any version can be restored by hand."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps({"t": _now(), "note": note, "playbook": pb}) + "\n")


def active(pb: dict) -> list[dict]:
    return [r for r in pb.get("rules", []) if r.get("status") == "active"]


def apply_changes(pb: dict, add: list[dict], retire: list[str],
                  baseline: dict, trades_seen: int) -> dict:
    """Apply a review's proposals, validating every addition.

    `baseline` is the track record at the moment of the change: without it a
    later review has nothing to compare against and cannot tell whether its own
    rule helped or the market simply changed.
    """
    pb = json.loads(json.dumps(pb))  # deep copy; the caller keeps the original
    _archive(pb, f"before review at {trades_seen} closed trades")
    result = {"added": [], "retired": [], "rejected": []}

    by_id = {r["id"]: r for r in pb["rules"]}
    for rid in retire or []:
        r = by_id.get(rid)
        if r and r.get("status") == "active":
            r["status"] = "retired"
            r["retired_at"] = _now()
            result["retired"].append(rid)

    next_n = max([int(r["id"][1:]) for r in pb["rules"] if r["id"][1:].isdigit()] or [0])
    for prop in add or []:
        text = (prop.get("rule") or "").strip()
        ok, why = validate_rule(text)
        if not ok:
            result["rejected"].append({"rule": text[:120], "reason": why})
            continue
        next_n += 1
        pb["rules"].append({
            "id": f"r{next_n}",
            "rule": text,
            "rationale": (prop.get("rationale") or "").strip()[:400],
            "hypothesis": (prop.get("hypothesis") or "").strip()[:300],
            "status": "active",
            "created_at": _now(),
            "created_after_trades": trades_seen,
            "baseline": baseline,
        })
        result["added"].append(f"r{next_n}")

    # Oldest-first retirement once the cap is hit: a rule that has survived
    # several reviews has earned its place more than one added last week.
    act = active(pb)
    if len(act) > MAX_ACTIVE_RULES:
        for r in act[: len(act) - MAX_ACTIVE_RULES]:
            r["status"] = "retired"
            r["retired_at"] = _now()
            r["retired_reason"] = "playbook full - oldest rule dropped"
            result["retired"].append(r["id"])

    pb["version"] = int(pb.get("version", 0)) + 1
    pb["updated_at"] = _now()
    pb["last_review_trades"] = trades_seen
    save(pb)
    return result


def summarize(pb: dict) -> str:
    """The playbook block injected into the analyst's prompt."""
    rules = active(pb)
    if not rules:
        return ""
    lines = [
        "",
        f"YOUR OWN PLAYBOOK (v{pb.get('version', 0)} - rules you wrote for yourself "
        f"from your own results; follow them unless you explain why not):",
    ]
    for r in rules:
        lines.append(f"  [{r['id']}] {r['rule']}")
    lines.append(
        "  These came from reviewing your own closed positions. If one is wrong, "
        "say so in your memo - the next review reads that and can retire it."
    )
    return "\n".join(lines)


def rules_for_review(pb: dict, current: dict) -> str:
    """Each active rule with its evidence, so a review can judge its own work.

    `current` is the present track record. Comparing it against the baseline
    stored with each rule is the closest thing available to a controlled test -
    honestly weak, because the market changed too, which the reviewer is told.
    """
    rules = active(pb)
    if not rules:
        return "  (no rules yet - this is the first review)"
    cur_wr = (current.get("overall") or {}).get("win_rate")
    cur_ar = (current.get("overall") or {}).get("avg_return_pct")
    out = []
    for r in rules:
        b = r.get("baseline") or {}
        bits = [f"[{r['id']}] {r['rule']}"]
        if r.get("hypothesis"):
            bits.append(f"      predicted: {r['hypothesis']}")
        if b.get("win_rate") is not None and cur_wr is not None:
            n_since = (current.get("total", 0)) - r.get("created_after_trades", 0)
            bits.append(
                f"      when written: {b['win_rate']:.0f}% winners, "
                f"{b.get('avg_return_pct', 0):+.2f}% avg | now: {cur_wr:.0f}% winners, "
                f"{cur_ar:+.2f}% avg | {n_since} positions closed since"
            )
        out.append("\n".join(bits))
    return "\n".join(out)
