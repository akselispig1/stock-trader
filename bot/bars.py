"""Shared maths over daily bar series.

Three modules need the same primitives - the benchmark comparison, the
volatility-based position sizer, and the market-regime read - so they live here
rather than being reimplemented (and drifting) in each.

Everything takes Alpaca's bar dicts (`{"c": close, ...}`) and is defensive about
short, missing or malformed series: a brand-new account, a thinly traded symbol
or a data outage should degrade to "no opinion", never to a wrong number.
"""
from __future__ import annotations

import math

TRADING_DAYS = 252  # for annualising a daily volatility


def f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def closes(bars: list[dict]) -> list[float]:
    """Positive closing prices, in order. Zero/absent closes are dropped."""
    return [c for c in (f(b.get("c")) for b in bars or []) if c > 0]


def daily_returns(bars: list[dict]) -> list[float]:
    """Daily fractional returns from a bar series."""
    cs = closes(bars)
    return [(cs[i] - cs[i - 1]) / cs[i - 1] for i in range(1, len(cs))]


def pearson(xs: list[float], ys: list[float], min_pairs: int = 8) -> float | None:
    """Correlation of two series, aligned on their most recent overlapping days.

    None when there is too little data, or when either series is flat - a
    zero-variance series has no defined correlation.
    """
    n = min(len(xs), len(ys))
    if n < min_pairs:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def stdev(xs: list[float]) -> float | None:
    """Sample standard deviation. None below two points."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def annualized_vol(bars: list[dict], window: int = 20) -> float | None:
    """Annualised volatility (percent) from the most recent `window` returns."""
    rs = daily_returns(bars)[-window:]
    if len(rs) < max(5, window // 2):  # too few days to mean anything
        return None
    sd = stdev(rs)
    return sd * math.sqrt(TRADING_DAYS) * 100.0 if sd else None


def sma(bars: list[dict], window: int) -> float | None:
    """Simple moving average of the last `window` closes."""
    cs = closes(bars)
    if len(cs) < window:
        return None
    return sum(cs[-window:]) / window


def percentile_rank(value: float, population: list[float]) -> float | None:
    """Where `value` sits within `population`, as 0-100.

    Used to say whether today's volatility is high *for this market* rather than
    against a hardcoded threshold that would be wrong in a different regime.
    """
    pop = [p for p in population if p is not None]
    if len(pop) < 20:
        return None
    below = sum(1 for p in pop if p < value)
    return below / len(pop) * 100.0
