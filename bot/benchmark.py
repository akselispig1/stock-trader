"""Benchmark tracking: is the bot beating the market, or just riding it?

Absolute P&L flatters any long book in a rising market. "+$17 this week" says
nothing on its own - if the index rose 2% over the same days, a $1,000 book
that gained $17 (1.7%) actually LOST to simply buying SPY and doing nothing,
and it paid AI costs for the privilege.

This module answers the two questions the bot needs to be able to see about
itself, and feeds both back into its own context each cycle:

1. ALPHA - the excess return over buy-and-hold in the benchmark, measured from
   the same baseline date, after AI operating cost. This is the only number
   that says whether the AI added anything.

2. DIFFERENTIATION - a book whose holdings move with the index cannot beat the
   index. Expected return is then the index return minus costs, no matter how
   sharp the research reads. Correlation is computed from actual daily bars
   rather than assumed, so it reflects the book as actually held.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# Correlation at or above this means the book is effectively an index tracker.
INDEX_LIKE = 0.85
# Below this, holdings genuinely diversify away from the benchmark.
DIFFERENTIATED = 0.70
# Pearson needs a reasonable sample; fewer paired days than this and we abstain
# rather than report a number that is mostly noise.
MIN_PAIRED_DAYS = 8


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _closes(bars: list[dict]) -> list[float]:
    return [c for c in (_f(b.get("c")) for b in bars or []) if c > 0]


def _returns(bars: list[dict]) -> list[float]:
    """Daily fractional returns from a bar series."""
    cs = _closes(bars)
    return [(cs[i] - cs[i - 1]) / cs[i - 1] for i in range(1, len(cs))]


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation of two return series, aligned on their most recent days.

    Returns None when there is too little data or either series is flat (a
    zero-variance series has no defined correlation).
    """
    n = min(len(xs), len(ys))
    if n < MIN_PAIRED_DAYS:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def record_baseline(ledger: dict, equity: float, bench_price: float | None,
                    symbol: str) -> None:
    """Stamp the measurement period's starting point into the ledger.

    Called when the equity baseline is first set (or reset). The benchmark
    price is recorded at the same moment so both series start on the same day;
    a benchmark baseline captured later would silently understate or overstate
    alpha by the drift in between.
    """
    ledger["baseline_equity"] = equity
    ledger["baseline_at"] = datetime.now(timezone.utc).isoformat()
    ledger["benchmark_symbol"] = symbol
    if bench_price and bench_price > 0:
        ledger["benchmark_baseline_price"] = bench_price


def backfill_baseline_price(ledger: dict, bench_price: float | None,
                            symbol: str) -> bool:
    """Attach a benchmark baseline to a ledger that predates this module.

    Returns True if the ledger was modified. The resulting alpha is measured
    from *today* rather than the true baseline date, so callers should mark it
    as partial - see `compute`'s `from_inception` flag.
    """
    if ledger.get("benchmark_baseline_price") or not bench_price or bench_price <= 0:
        return False
    ledger["benchmark_symbol"] = symbol
    ledger["benchmark_baseline_price"] = bench_price
    ledger["benchmark_backfilled_at"] = datetime.now(timezone.utc).isoformat()
    return True


def compute(ledger: dict, equity: float, net_pnl: float,
            bench_price: float | None, capital_base: float | None = None) -> dict | None:
    """Bot return vs benchmark return over the same period.

    `net_pnl` is gross P&L already reduced by cumulative AI cost, so net alpha
    is the honest figure: what the AI earned above the index after paying for
    itself.

    `capital_base` is the money the bot actually manages, and is the correct
    denominator for a percentage return. It matters because the equity baseline
    is the whole brokerage account: with a CAPITAL_CAP of 1,000 against a
    100,000 paper balance, dividing by the baseline understates every return by
    100x and makes alpha read as failure however well the book does. Falls back
    to the baseline only when no cap is set, where the two are the same thing.
    """
    baseline = _f(ledger.get("baseline_equity"))
    base_price = _f(ledger.get("benchmark_baseline_price"))
    if baseline <= 0 or base_price <= 0 or not bench_price or bench_price <= 0:
        return None
    denom = _f(capital_base) if _f(capital_base) > 0 else baseline

    gross_pnl = equity - baseline
    bot_pct = gross_pnl / denom * 100.0
    net_pct = net_pnl / denom * 100.0
    bench_pct = (bench_price - base_price) / base_price * 100.0

    return {
        "symbol": ledger.get("benchmark_symbol") or "SPY",
        "baseline_at": ledger.get("baseline_at"),
        # A backfilled baseline means the comparison starts from the backfill
        # date, not from when the book actually began.
        "from_inception": not ledger.get("benchmark_backfilled_at"),
        "baseline_price": base_price,
        "current_price": bench_price,
        "bot_return_pct": bot_pct,
        "net_return_pct": net_pct,
        "benchmark_return_pct": bench_pct,
        "alpha_pct": bot_pct - bench_pct,
        "net_alpha_pct": net_pct - bench_pct,
        # The same starting capital left in the benchmark, for a plain-dollar
        # comparison that needs no percentage arithmetic to read.
        "capital_base": denom,
        "buy_and_hold_value": denom * (bench_price / base_price),
        "book_value": denom + net_pnl,
    }


def book_correlation(bars: dict[str, list[dict]], positions: list[dict],
                     symbol: str = "SPY") -> dict | None:
    """Value-weighted correlation of the book's holdings to the benchmark.

    Each holding's daily returns are correlated against the benchmark's over
    the overlapping window, then weighted by market value. A holding with no
    usable bar data is excluded from the weighting rather than assumed to be
    uncorrelated, which would flatter the result.
    """
    bench = _returns(bars.get(symbol) or [])
    if len(bench) < MIN_PAIRED_DAYS or not positions:
        return None

    per_symbol: dict[str, float] = {}
    weighted_sum = 0.0
    covered_value = 0.0
    total_value = sum(_f(p.get("market_value")) for p in positions)

    for p in positions:
        sym = p.get("symbol")
        value = _f(p.get("market_value"))
        if value <= 0:
            continue
        # The benchmark correlates perfectly with itself; no need for bars.
        corr = 1.0 if sym == symbol else pearson(_returns(bars.get(sym) or []), bench)
        if corr is None:
            continue
        per_symbol[sym] = corr
        weighted_sum += corr * value
        covered_value += value

    if covered_value <= 0:
        return None

    return {
        "symbol": symbol,
        "weighted": weighted_sum / covered_value,
        "per_symbol": per_symbol,
        # What fraction of the book this figure actually speaks for.
        "coverage_pct": (covered_value / total_value * 100.0) if total_value > 0 else 0.0,
    }


def _verdict(corr: float) -> str:
    if corr >= INDEX_LIKE:
        return "INDEX-LIKE"
    if corr >= DIFFERENTIATED:
        return "PARTLY DIFFERENTIATED"
    return "DIFFERENTIATED"


def summarize(perf: dict | None, corr: dict | None) -> str:
    """The benchmark reality-check block injected into the AI's context."""
    lines: list[str] = []

    if perf:
        sym = perf["symbol"]
        alpha = perf["net_alpha_pct"]
        lines += [
            "",
            f"BENCHMARK REALITY CHECK (vs buy-and-hold {sym}):",
            f"  Your book:        {perf['bot_return_pct']:+.2f}%  "
            f"(after AI cost: {perf['net_return_pct']:+.2f}%)",
            f"  {sym} buy-and-hold: {perf['benchmark_return_pct']:+.2f}%",
            f"  NET ALPHA:        {alpha:+.2f} percentage points",
        ]
        if not perf["from_inception"]:
            lines.append("  (benchmark measured from a later date than the book's "
                         "own baseline - treat as approximate)")
        if alpha < 0:
            lines.append(
                f"  ⚠ You are BEHIND the market. Doing nothing but holding {sym} "
                f"would have produced more. Every trade you make must be "
                f"justified against that alternative, not against zero."
            )
        else:
            lines.append(
                f"  ✓ Ahead of {sym} for now. Protect this by not churning: "
                f"each round trip costs spread plus AI time."
            )

    if corr:
        c = corr["weighted"]
        names = " · ".join(
            f"{s} {v:.2f}" for s, v in
            sorted(corr["per_symbol"].items(), key=lambda kv: -kv[1])
        )
        lines += [
            "",
            f"BOOK CORRELATION TO {corr['symbol']}: {c:.2f} "
            f"({_verdict(c)}, covering {corr['coverage_pct']:.0f}% of book)",
            f"  {names}",
        ]
        if c >= INDEX_LIKE:
            lines.append(
                "  ⚠ THIS BOOK IS THE INDEX. Holdings this correlated will return "
                "roughly what the index returns, minus your costs - you cannot beat "
                "a benchmark by holding it. Mega-cap tech names ARE the index's "
                "largest weights; owning several of them plus an index ETF is one "
                "bet, not a diversified book. To have any chance of alpha, hold "
                "things that behave DIFFERENTLY: smaller caps, out-of-favour "
                "sectors, defensives, commodities - names where your specific "
                "thesis, not the market's direction, decides the outcome."
            )
        elif c >= DIFFERENTIATED:
            lines.append(
                "  Partly differentiated. Some independent exposure, but the "
                "index still drives most of the book's movement."
            )
        else:
            lines.append(
                "  Genuinely differentiated - outcomes here depend on your theses "
                "rather than the market's direction. This is where alpha can exist."
            )

    return "\n".join(lines)
