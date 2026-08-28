"""Per-position thesis memory + a readable research journal.

Two problems this solves:

1. The bot had amnesia. Each cycle it re-derived a view from scratch, so it
   never asked "is the reason I bought this still true? did I hit my target?".
   `theses.json` stores, per holding: why it was bought, the entry price, a
   target and stop, and a conviction level. That is fed back into the research
   context every cycle so the AI judges each position AGAINST ITS OWN PLAN.

2. Nothing was readable after the fact. `journal.jsonl` appends every cycle's
   summary + decisions so both you and the AI can look back over what was
   researched and concluded.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "data")
THESES_PATH = os.path.join(DATA_DIR, "theses.json")
JOURNAL_PATH = os.path.join(DATA_DIR, "journal.jsonl")

MAX_JOURNAL_ENTRIES = 200  # keep the file small enough to commit + re-read


def load_theses() -> dict:
    try:
        with open(THESES_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_theses(theses: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(THESES_PATH, "w") as f:
        json.dump(theses, f, indent=2)


def record_entry(theses: dict, order: dict, fill_price: float | None = None) -> None:
    """Store/refresh the thesis for a symbol the bot just bought."""
    sym = order["symbol"]
    now = datetime.now(timezone.utc).isoformat()
    existing = theses.get(sym, {})
    theses[sym] = {
        "symbol": sym,
        "thesis": order.get("thesis") or order.get("reasoning") or existing.get("thesis", ""),
        "conviction": order.get("conviction") or existing.get("conviction", "medium"),
        "target_price": order.get("target_price") or existing.get("target_price"),
        "stop_price": order.get("stop_price") or existing.get("stop_price"),
        "entry_price": existing.get("entry_price") or fill_price,
        "opened_at": existing.get("opened_at", now),
        "updated_at": now,
    }


def prune(theses: dict, held_symbols: set[str]) -> dict:
    """Drop theses for positions that are no longer held (sold/closed)."""
    return {s: t for s, t in theses.items() if s in held_symbols}


def summarize(theses: dict, positions: list[dict]) -> str:
    """A context block asking the AI to judge each holding against its own plan."""
    if not positions:
        return ""
    lines = ["", "YOUR OWN THESES FOR CURRENT HOLDINGS (judge each against ITS OWN plan):"]
    for p in positions:
        sym = p["symbol"]
        t = theses.get(sym)
        try:
            price = float(p.get("current_price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if not t:
            lines.append(f"  {sym}: (no recorded thesis - state one now, with a target and stop)")
            continue
        tgt = t.get("target_price")
        stop = t.get("stop_price")
        bits = [f"bought {str(t.get('opened_at', ''))[:10]}", f"conviction {t.get('conviction', 'n/a')}"]
        if tgt:
            hit = " ** TARGET HIT **" if price and price >= float(tgt) else ""
            bits.append(f"target ${float(tgt):,.2f}{hit}")
        if stop:
            brk = " ** STOP BREACHED **" if price and price <= float(stop) else ""
            bits.append(f"stop ${float(stop):,.2f}{brk}")
        lines.append(f"  {sym} ({', '.join(bits)}): {t.get('thesis', '')}")
    lines.append(
        "  For each: is the thesis STILL TRUE? Has it hit its target (take profit) or "
        "broken its stop/thesis (cut)? Say so explicitly."
    )
    return "\n".join(lines)


def append_journal(entry: dict) -> None:
    """Append one cycle to the readable research journal (trimmed to a max size)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    rows = []
    try:
        with open(JOURNAL_PATH) as f:
            rows = [line for line in f.read().splitlines() if line.strip()]
    except FileNotFoundError:
        pass
    rows.append(json.dumps(entry))
    rows = rows[-MAX_JOURNAL_ENTRIES:]
    with open(JOURNAL_PATH, "w") as f:
        f.write("\n".join(rows) + "\n")


def recent_journal(n: int = 5) -> str:
    """A compact digest of recent cycles, so the AI has continuity of its own thinking."""
    try:
        with open(JOURNAL_PATH) as f:
            rows = [json.loads(line) for line in f.read().splitlines() if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    rows = rows[-n:]
    if not rows:
        return ""
    lines = ["", f"YOUR RECENT DECISIONS (last {len(rows)} cycles - avoid contradicting "
             "yourself without saying why):"]
    for r in rows:
        acts = r.get("actions") or "no action"
        lines.append(f"  [{str(r.get('t', ''))[:16]}] {acts} - {str(r.get('summary', ''))[:180]}")
    return "\n".join(lines)
