"""Decision scorecard: grade closed positions and show the bot its own record.

The bot has no memory between cycles - every run is a fresh API call that reads
context from disk and then forgets. So it cannot get better on its own; the most
we can do is make the EVIDENCE of its past decisions visible, in a form compact
enough to re-read every cycle without burying the rest of the context.

When a position closes, this records what was claimed when it was opened (the
thesis, target, stop and conviction) against what actually happened. The digest
fed back into the prompt answers questions the bot could not otherwise ask:

  - Is my "high conviction" actually better than my "starter" positions, or do I
    just talk more confidently about some of them?
  - Do I take profits at my targets, or let winners round-trip?
  - Do I cut at my stops, or hold losers and hope?

`closed.jsonl` keeps the full history for you and the dashboard; only the
aggregate goes into the prompt.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "data")
CLOSED_PATH = os.path.join(DATA_DIR, "closed.jsonl")

MAX_CLOSED = 500  # plenty of history, still a small file
# Below this many closed trades the statistics are noise. Saying "you win 100%
# of the time" after one lucky trade would actively mislead the model.
MIN_FOR_STATS = 5

CONVICTIONS = ("high", "medium", "starter")


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def classify_exit(exit_price: float, target: float | None, stop: float | None) -> str:
    """How the position actually ended, versus the plan it was opened with."""
    if target and exit_price >= _f(target):
        return "target_hit"
    if stop and exit_price <= _f(stop):
        return "stop_hit"
    return "discretionary"


def load_closed() -> list[dict]:
    try:
        with open(CLOSED_PATH) as f:
            return [json.loads(l) for l in f.read().splitlines() if l.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append(rows: list[dict]) -> None:
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    existing = load_closed()
    existing.extend(rows)
    existing = existing[-MAX_CLOSED:]
    with open(CLOSED_PATH, "w") as f:
        f.write("\n".join(json.dumps(r) for r in existing) + "\n")


def record_closures(theses: dict, held_symbols: set[str],
                    exit_prices: dict[str, float]) -> list[dict]:
    """Grade every thesis whose position is no longer held.

    `exit_prices` should carry the last known price per symbol, captured BEFORE
    the position was closed - once Alpaca drops the position there is no price
    left to read. A symbol with no usable entry or exit price is still recorded,
    flagged `priced: false`, so the count of closures stays honest even when the
    return cannot be computed; those rows are excluded from return statistics.
    """
    closed: list[dict] = []
    for sym, t in theses.items():
        if sym in held_symbols:
            continue
        entry = _f(t.get("entry_price"))
        exit_price = _f(exit_prices.get(sym))
        opened = _parse(t.get("opened_at"))
        now = _now()
        priced = entry > 0 and exit_price > 0
        row = {
            "symbol": sym,
            "opened_at": t.get("opened_at"),
            "closed_at": now.isoformat(),
            "hold_days": round((now - opened).total_seconds() / 86400, 2) if opened else None,
            "conviction": (t.get("conviction") or "medium").lower(),
            "thesis": t.get("thesis") or "",
            "target_price": t.get("target_price"),
            "stop_price": t.get("stop_price"),
            "entry_price": entry or None,
            "exit_price": exit_price or None,
            "priced": priced,
            "return_pct": round((exit_price - entry) / entry * 100, 3) if priced else None,
            "outcome": classify_exit(exit_price, t.get("target_price"), t.get("stop_price"))
                       if priced else "unknown",
        }
        row["won"] = (row["return_pct"] or 0) > 0 if priced else None
        closed.append(row)
    _append(closed)
    return closed


def _bucket(rows: list[dict]) -> dict:
    """Win rate / average return over rows that have a computable return."""
    scored = [r for r in rows if r.get("priced") and r.get("return_pct") is not None]
    if not scored:
        return {"n": len(rows), "scored": 0}
    wins = [r for r in scored if r["won"]]
    holds = [r["hold_days"] for r in scored if r.get("hold_days") is not None]
    return {
        "n": len(rows),
        "scored": len(scored),
        "win_rate": len(wins) / len(scored) * 100,
        "avg_return_pct": sum(r["return_pct"] for r in scored) / len(scored),
        "avg_win_pct": sum(r["return_pct"] for r in wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": (sum(r["return_pct"] for r in scored if not r["won"])
                         / max(1, len(scored) - len(wins))) if len(scored) > len(wins) else 0.0,
        "avg_hold_days": sum(holds) / len(holds) if holds else None,
    }


def stats(rows: list[dict] | None = None) -> dict:
    """Aggregate record: overall, split by conviction, and how exits happened."""
    rows = load_closed() if rows is None else rows
    if not rows:
        return {"total": 0}
    by_conv = {c: _bucket([r for r in rows if r.get("conviction") == c]) for c in CONVICTIONS}
    outcomes = {}
    for r in rows:
        outcomes[r.get("outcome", "unknown")] = outcomes.get(r.get("outcome", "unknown"), 0) + 1
    return {
        "total": len(rows),
        "enough_data": len([r for r in rows if r.get("priced")]) >= MIN_FOR_STATS,
        "overall": _bucket(rows),
        "by_conviction": {c: v for c, v in by_conv.items() if v.get("n")},
        "outcomes": outcomes,
    }


def summarize(s: dict | None = None) -> str:
    """The compact digest injected into the prompt. Deliberately ~10 lines."""
    s = stats() if s is None else s
    total = s.get("total", 0)
    if not total:
        return ""

    lines = ["", f"YOUR TRACK RECORD ({total} closed position"
                 f"{'s' if total != 1 else ''}):"]

    if not s.get("enough_data"):
        lines += [
            f"  Only {total} closed so far - too few to draw conclusions from. "
            f"Do not treat this as evidence that your process works or does not.",
        ]

    o = s.get("overall", {})
    if o.get("scored"):
        hold = f", held {o['avg_hold_days']:.1f}d avg" if o.get("avg_hold_days") else ""
        lines.append(
            f"  Overall: {o['win_rate']:.0f}% winners, average {o['avg_return_pct']:+.2f}% "
            f"per position (wins {o['avg_win_pct']:+.1f}%, losses {o['avg_loss_pct']:+.1f}%){hold}"
        )

    for conv in CONVICTIONS:
        b = s.get("by_conviction", {}).get(conv)
        if b and b.get("scored"):
            lines.append(f"  {conv:<8}: {b['scored']} closed, {b['win_rate']:.0f}% winners, "
                         f"average {b['avg_return_pct']:+.2f}%")

    out = s.get("outcomes", {})
    if out:
        parts = [f"{v} {k.replace('_', ' ')}" for k, v in sorted(out.items(), key=lambda kv: -kv[1])]
        lines.append(f"  Exits: {', '.join(parts)}")

    # Turn the numbers into the specific question each pattern should prompt.
    if s.get("enough_data"):
        lines += _lessons(s)
    return "\n".join(lines)


def _lessons(s: dict) -> list[str]:
    """Name the pattern in the data rather than leaving the model to spot it."""
    out: list[str] = []
    conv = s.get("by_conviction", {})
    high, starter = conv.get("high", {}), conv.get("starter", {})
    if high.get("scored", 0) >= 3 and starter.get("scored", 0) >= 3:
        if high["avg_return_pct"] < starter["avg_return_pct"]:
            out.append(
                "  ⚠ Your HIGH conviction calls have done WORSE than your starters. "
                "Confidence is not tracking accuracy - size down until it does."
            )
        else:
            out.append(
                "  ✓ High conviction is outperforming starters, so your confidence "
                "carries real information. Keep sizing by it."
            )

    o = s.get("overall", {})
    if o.get("scored", 0) >= MIN_FOR_STATS:
        win, loss = o.get("avg_win_pct", 0), abs(o.get("avg_loss_pct", 0))
        if loss > win and win > 0:
            out.append(
                f"  ⚠ Your average loss ({loss:.1f}%) is bigger than your average win "
                f"({win:.1f}%). You are cutting winners early and holding losers - the "
                f"most common way a book bleeds. Respect your stops and let targets run."
            )

    outc = s.get("outcomes", {})
    disc = outc.get("discretionary", 0)
    planned = outc.get("target_hit", 0) + outc.get("stop_hit", 0)
    if disc > planned and disc >= 3:
        out.append(
            "  ⚠ Most of your exits were neither the target nor the stop you set. "
            "Either your targets and stops are unrealistic, or you are abandoning "
            "plans mid-trade. Set levels you will actually honour."
        )
    return out
