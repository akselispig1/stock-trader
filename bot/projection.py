"""What each RISK_LEVEL actually implies, before you choose one.

The presets are four rows of numbers - allocation cap, cash reserve, position
count - and none of them tell you the thing you want to know: how much will the
book swing, and how bad can it get? This turns the settings into that answer.

WHAT THIS PREDICTS, AND WHAT IT REFUSES TO

It predicts RISK: volatility, the range of plausible outcomes, and how deep the
drawdowns get. That is forecastable to a useful degree because volatility
persists - a portfolio of volatile names stays volatile next month.

It does NOT predict RETURN, and the simulation assumes zero drift precisely so
that no return forecast can sneak in through the back door. If it assumed the
market's long-run average it would produce cheerful numbers that are really just
that assumption echoed back. So read the median outcome as "roughly flat before
whatever the market and the bot's skill add", not as a forecast of nothing
happening.

HOW IT WORKS

Portfolio volatility from the real volatilities of the names the bot can hold
and their average pairwise correlation, then a Monte Carlo over a year of daily
moves. Correlation is what makes this non-obvious: twelve positions are not
twelve independent bets when they all fall together, which is exactly what
happens in a selloff.
"""
from __future__ import annotations

import math
import random

TRADING_DAYS = 252
DEFAULT_PATHS = 20000

# Used only when live volatility data is unavailable. Deliberately on the high
# side: the niche block (miners, uranium, biotech, single-country) is far more
# volatile than a broad index, and understating risk is the costlier error.
FALLBACK_VOL_PCT = 28.0
FALLBACK_CORR = 0.55


def portfolio_vol_pct(vols: list[float], n_positions: int, invested_frac: float,
                      avg_corr: float) -> float | None:
    """Annualised volatility of the whole book, cash included.

    Equal weights across `n_positions` names drawn from `vols`, combined under a
    constant average pairwise correlation, then scaled by how much of the book
    is actually invested (cash contributes no volatility).

    Exact for constant correlation:
        Var = w^2 * [ sum(s_i^2) + rho * ( sum(s_i)^2 - sum(s_i^2) ) ]
    """
    if not vols or n_positions <= 0 or invested_frac <= 0:
        return None
    n = min(n_positions, len(vols))
    # Take the middle of the volatility distribution rather than the extremes:
    # the bot holds a spread of names, not only the wildest or the calmest.
    ordered = sorted(vols)
    lo = max(0, (len(ordered) - n) // 2)
    sample = ordered[lo:lo + n]
    if len(sample) < n:  # fewer names available than positions targeted
        sample = (sample * n)[:n]

    w = 1.0 / n
    s1 = sum(sample)
    s2 = sum(s * s for s in sample)
    var = w * w * (s2 + avg_corr * (s1 * s1 - s2))
    return math.sqrt(max(0.0, var)) * invested_frac


def average_correlation(bars: dict[str, list[dict]], symbols: list[str],
                        max_pairs: int = 300) -> float | None:
    """Mean pairwise correlation across the tradeable universe.

    Sampled rather than exhaustive: with 56 symbols there are 1,540 pairs, and
    a few hundred give the same answer for a fraction of the work.
    """
    from .bars import daily_returns, pearson

    series = {}
    for s in symbols:
        r = daily_returns(bars.get(s) or [])
        if len(r) >= 8:
            series[s] = r
    names = sorted(series)
    if len(names) < 3:
        return None

    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
    if len(pairs) > max_pairs:
        random.Random(0).shuffle(pairs)   # fixed seed: same answer every run
        pairs = pairs[:max_pairs]

    vals = [c for c in (pearson(series[a], series[b]) for a, b in pairs) if c is not None]
    return sum(vals) / len(vals) if vals else None


def simulate(annual_vol_pct: float, capital: float, days: int = TRADING_DAYS,
             paths: int = DEFAULT_PATHS, seed: int = 7) -> dict:
    """Monte Carlo a year of daily moves at this volatility, with ZERO drift.

    Zero drift is the point: this measures the shape of the risk, not a view on
    returns. Reported drawdowns are peak-to-trough within the year, which is the
    number that actually decides whether someone can hold on through it.
    """
    rng = random.Random(seed)
    daily = (annual_vol_pct / 100.0) / math.sqrt(TRADING_DAYS)
    ends, drawdowns = [], []

    for _ in range(paths):
        value = 1.0
        peak = 1.0
        worst = 0.0
        for _ in range(days):
            value *= (1.0 + rng.gauss(0.0, daily))
            peak = max(peak, value)
            worst = max(worst, (peak - value) / peak)
        ends.append(value)
        drawdowns.append(worst)

    ends.sort()
    drawdowns.sort()

    def q(xs, p):
        return xs[min(len(xs) - 1, max(0, int(p * len(xs))))]

    return {
        "annual_vol_pct": annual_vol_pct,
        "capital": capital,
        # Where the book plausibly ends the year, before any return the market
        # or the bot's skill adds.
        "p05_value": capital * q(ends, 0.05),
        "p50_value": capital * q(ends, 0.50),
        "p95_value": capital * q(ends, 0.95),
        "p05_pct": (q(ends, 0.05) - 1) * 100,
        "p50_pct": (q(ends, 0.50) - 1) * 100,
        "p95_pct": (q(ends, 0.95) - 1) * 100,
        # Worst peak-to-trough dip DURING the year - almost always deeper than
        # the end-of-year loss, and the number people actually panic at.
        "median_drawdown_pct": q(drawdowns, 0.50) * 100,
        "bad_drawdown_pct": q(drawdowns, 0.90) * 100,
        "worst_drawdown_pct": q(drawdowns, 0.99) * 100,
        "prob_down_10": sum(1 for d in drawdowns if d >= 0.10) / len(drawdowns) * 100,
        "prob_down_20": sum(1 for d in drawdowns if d >= 0.20) / len(drawdowns) * 100,
        "prob_down_30": sum(1 for d in drawdowns if d >= 0.30) / len(drawdowns) * 100,
    }


def project(preset: dict, vols: list[float], capital: float, avg_corr: float,
            paths: int = DEFAULT_PATHS) -> dict | None:
    """Risk projection for one RISK_LEVEL preset."""
    invested = 1.0 - (preset["reserve"] / 100.0)
    n = int(preset["positions"])

    # The per-symbol cap can make the target position count unreachable: n names
    # at equal weight need 100/n% each, and if that exceeds the cap the book
    # simply cannot be as concentrated as the count implies.
    min_positions = math.ceil(100.0 / preset["alloc"]) if preset["alloc"] > 0 else n
    effective_n = max(n, min_positions) if invested >= 0.999 else n

    vol = portfolio_vol_pct(vols, effective_n, invested, avg_corr)
    if vol is None:
        return None
    out = simulate(vol, capital, paths=paths)
    out.update({
        "positions": n,
        "effective_positions": effective_n,
        "invested_pct": invested * 100,
        "alloc_cap_pct": preset["alloc"],
        "reserve_pct": preset["reserve"],
        "avg_corr": avg_corr,
    })
    return out


def project_all(presets: dict[str, dict], vols: list[float], capital: float,
                avg_corr: float, paths: int = DEFAULT_PATHS) -> dict[str, dict]:
    return {name: p for name, p in
            ((k, project(v, vols, capital, avg_corr, paths)) for k, v in presets.items())
            if p}


def summarize(projections: dict[str, dict], active: str, currency: str = "CHF") -> str:
    """Human-readable comparison of the presets."""
    if not projections:
        return "No projection available (not enough price history yet)."

    any_p = next(iter(projections.values()))
    lines = [
        f"RISK PROJECTION - {any_p['capital']:,.0f} {currency} over one year",
        f"(assumes ZERO market drift, so these are RISK figures, not return "
        f"forecasts; average correlation between holdings {any_p['avg_corr']:.2f})",
        "",
        f"{'level':<11}{'swing':>7}{'typical dip':>13}{'bad year dip':>14}"
        f"{'down 20%+':>11}{'range of outcomes':>26}",
    ]
    for name, p in projections.items():
        mark = " ←" if name == active else ""
        rng = (f"{p['p05_value']:,.0f} to {p['p95_value']:,.0f}")
        lines.append(
            f"{name:<11}{p['annual_vol_pct']:>6.0f}%{p['median_drawdown_pct']:>12.0f}%"
            f"{p['bad_drawdown_pct']:>13.0f}%{p['prob_down_20']:>10.0f}%"
            f"{rng:>26}{mark}"
        )
    lines += [
        "",
        "swing        = annual volatility of the whole book, cash included",
        "typical dip  = worst peak-to-trough fall in a median year",
        "bad year dip = worst fall in a bad-but-not-extreme year (worst 1 in 10)",
        "down 20%+    = chance of being down 20% at SOME point during the year",
        "",
        "The dip columns matter more than the range: they are what you would "
        "actually have to sit through without switching it off.",
    ]
    return "\n".join(lines)


def main() -> int:
    """`python -m bot.projection` - compare the risk levels on live data."""
    import argparse

    from .alpaca import Alpaca, AlpacaError
    from .bars import annualized_vol
    from .config import RISK_PRESETS, Config

    ap = argparse.ArgumentParser(
        description="Project what each RISK_LEVEL implies for risk (not return).")
    ap.add_argument("--capital", type=float, default=None,
                    help="Capital to project (default: your CAPITAL_CAP).")
    ap.add_argument("--paths", type=int, default=DEFAULT_PATHS,
                    help=f"Monte Carlo paths (default {DEFAULT_PATHS}).")
    args = ap.parse_args()

    cfg = Config()
    cfg.validate()
    capital = args.capital or cfg.capital_cap or 1000.0

    print(f"Fetching price history for {len(cfg.watchlist)} symbols...")
    bars: dict[str, list[dict]] = {}
    try:
        bars = Alpaca(cfg).daily_bars(cfg.watchlist, limit=90)
    except AlpacaError as e:
        print(f"  could not reach Alpaca ({e})")

    vols = [v for v in (annualized_vol(bars.get(s) or []) for s in cfg.watchlist)
            if v is not None]
    corr = average_correlation(bars, cfg.watchlist)

    if vols and corr is not None:
        lo, hi = min(vols), max(vols)
        print(f"  {len(vols)} symbols priced · volatility {lo:.0f}%-{hi:.0f}%/yr "
              f"· average correlation {corr:.2f}\n")
    else:
        # Be explicit that the numbers below rest on assumptions, not data.
        vols = vols or [FALLBACK_VOL_PCT] * 12
        corr = corr if corr is not None else FALLBACK_CORR
        print(f"  ⚠ Not enough live data - using assumed {FALLBACK_VOL_PCT:.0f}% "
              f"volatility and {FALLBACK_CORR:.2f} correlation. Treat as "
              f"illustrative only.\n")

    print(summarize(project_all(RISK_PRESETS, vols, capital, corr, paths=args.paths),
                    active=cfg.risk_level, currency=cfg.capital_currency))
    print(f"\nYour current setting is RISK_LEVEL={cfg.risk_level}. Change it in "
          f".env (or the repo Variable) and restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
