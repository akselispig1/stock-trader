"""Market regime: what kind of market is this, and how should the bot behave?

The bot previously acted identically in a calm uptrend and a volatile selloff.
Those call for different behaviour: adding into strength is reasonable when the
market is trending and quiet, and a good way to lose money when it is falling
and violent.

Two cheap, honest readings off the benchmark's own price history:

  TREND  - benchmark versus its 200-day average. A crude but durable line
           between "the market is generally going up" and "it isn't".
  STRESS - current volatility as a PERCENTILE of its own past year, not against
           a fixed threshold. 20% annualised is calm for one market and alarming
           for another; ranking it against its own history travels better.

This is deliberately not a forecast. It never says what will happen - it says
what kind of market this currently is, and what posture that argues for.
"""
from __future__ import annotations

from .bars import annualized_vol, closes, percentile_rank, sma

TREND_WINDOW = 200
VOL_WINDOW = 20
# Enough history for a percentile to mean something without demanding a
# full year, which a new data feed may not have.
MIN_HISTORY = 120

CALM, NORMAL, STRESSED = "calm", "normal", "stressed"
UPTREND, DOWNTREND = "uptrend", "downtrend"


def _stress(pct_rank: float | None) -> str:
    if pct_rank is None:
        return NORMAL
    if pct_rank >= 80:
        return STRESSED
    if pct_rank <= 40:
        return CALM
    return NORMAL


def detect(bars: list[dict], symbol: str = "SPY") -> dict | None:
    """Classify the current regime from the benchmark's daily bars.

    Returns None when there is too little history to say anything, which is the
    correct answer on a new account rather than a confident guess.
    """
    cs = closes(bars)
    if len(cs) < MIN_HISTORY:
        return None

    price = cs[-1]
    # With less than the full 200 days, fall back to the longest average the
    # data supports rather than reporting no trend at all.
    window = min(TREND_WINDOW, len(cs))
    avg = sma(bars, window)
    if not avg:
        return None
    trend = UPTREND if price >= avg else DOWNTREND
    distance_pct = (price - avg) / avg * 100.0

    vol = annualized_vol(bars, window=VOL_WINDOW)
    # Rank today's volatility against its own PAST. The range stops one short of
    # the end deliberately: including today in the population it is being
    # measured against pulls every reading toward the middle, so a genuine spike
    # would rank lower than it deserves.
    history = []
    for end in range(MIN_HISTORY, len(bars)):
        v = annualized_vol(bars[:end], window=VOL_WINDOW)
        if v is not None:
            history.append(v)
    rank = percentile_rank(vol, history) if vol is not None else None
    stress = _stress(rank)

    return {
        "symbol": symbol,
        "trend": trend,
        "sma_window": window,
        "price": price,
        "sma": avg,
        "distance_pct": distance_pct,
        "vol_pct": vol,
        "vol_percentile": rank,
        "stress": stress,
        "label": f"{trend}/{stress}",
        "risk_on": trend == UPTREND and stress != STRESSED,
    }


# What each combination argues for. Phrased as posture, never as prediction.
POSTURE = {
    (UPTREND, CALM): (
        "Trending and quiet - the most favourable backdrop for adding risk. "
        "Deploy into good ideas and let winners run toward their targets."),
    (UPTREND, NORMAL): (
        "Trending with ordinary volatility. Normal posture: add on genuine "
        "conviction, keep sizes at target weight."),
    (UPTREND, STRESSED): (
        "Rising but volatile - often a late or fragile trend. Keep positions "
        "toward the smaller end and be quicker to honour stops; a violent tape "
        "will hit a wide stop on noise alone."),
    (DOWNTREND, CALM): (
        "Below trend but settled - frequently where value appears. Selective "
        "buying of genuinely cheap names is reasonable; do not chase strength."),
    (DOWNTREND, NORMAL): (
        "Below trend. Be more demanding of new positions: in a falling market, "
        "most names fall regardless of their individual merits."),
    (DOWNTREND, STRESSED): (
        "Falling and volatile - the worst backdrop for adding risk. Correlations "
        "rise toward 1 in a selloff, so diversification helps least exactly when "
        "you need it most. Prioritise protecting capital, hold more cash, and "
        "size any new position small. Doing nothing is a strong option."),
}


def summarize(r: dict | None) -> str:
    """Context block naming the regime and the posture it argues for."""
    if not r:
        return ""
    vol_bit = ""
    if r.get("vol_pct") is not None:
        vol_bit = f", volatility {r['vol_pct']:.0f}%/yr"
        if r.get("vol_percentile") is not None:
            vol_bit += f" ({r['vol_percentile']:.0f}th percentile of the past year)"

    return "\n".join([
        "",
        f"MARKET REGIME ({r['symbol']}): {r['label'].upper()}",
        f"  {r['symbol']} is {abs(r['distance_pct']):.1f}% "
        f"{'above' if r['distance_pct'] >= 0 else 'below'} its "
        f"{r['sma_window']}-day average{vol_bit}.",
        f"  {POSTURE.get((r['trend'], r['stress']), '')}",
        "  This describes the market you are trading in, not a forecast. Adjust "
        "how aggressively you act, not what you believe about individual names.",
    ])
