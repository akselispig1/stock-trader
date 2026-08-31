"""Volatility-adjusted position sizing: equal RISK, not equal dollars.

Sizing every position at the same dollar amount silently makes the book a bet
on whichever holdings happen to be most volatile. $83 of a name that swings 60%
a year contributes roughly three times the risk of $83 of one that swings 20%,
so an "equally weighted" book of twelve names can easily have half its risk in
two of them.

This scales each name's target weight by the inverse of its volatility, so a
calm name gets a larger dollar position and a wild one gets a smaller position
for the same contribution to how much the book moves.

Two deliberate limits:

- The scale factor is clamped. Unclamped inverse-volatility sizing would put an
  absurd amount into the quietest name on the list, which concentrates a
  different risk (being wrong about one thing) in the name of reducing this one.
- A symbol with no usable volatility gets the plain target weight, never a
  guess. Missing data must not silently become a sizing decision.
"""
from __future__ import annotations

from .bars import annualized_vol

# The volatility a "normal" holding is assumed to have. A name at exactly this
# level sizes to its plain target weight; calmer names scale up, wilder down.
# Roughly the long-run volatility of a broad equity index.
REFERENCE_VOL_PCT = 18.0

# How far sizing may deviate from equal weight. 0.5-1.5x keeps the adjustment
# meaningful without letting one quiet name dominate the book.
MIN_SCALE = 0.5
MAX_SCALE = 1.5

# Below this, treat volatility as unmeasured rather than as genuinely zero -
# a stale or flat series would otherwise scale to the maximum position.
MIN_MEANINGFUL_VOL = 1.0


def scale_for(vol_pct: float | None) -> float:
    """Size multiplier for a name at this annualised volatility."""
    if vol_pct is None or vol_pct < MIN_MEANINGFUL_VOL:
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, REFERENCE_VOL_PCT / vol_pct))


def vols(bars: dict[str, list[dict]], symbols: list[str],
         window: int = 20) -> dict[str, float]:
    """Annualised volatility per symbol, omitting those without enough data."""
    out = {}
    for s in symbols:
        v = annualized_vol(bars.get(s) or [], window=window)
        if v is not None:
            out[s] = v
    return out


def target_dollars(capital: float, target_positions: int, vol_pct: float | None) -> float:
    """Volatility-adjusted dollar size for one full-weight position."""
    if capital <= 0 or target_positions <= 0:
        return 0.0
    return capital / target_positions * scale_for(vol_pct)


def allocation_cap_pct(base_cap_pct: float, vol_pct: float | None) -> float:
    """Per-symbol allocation cap, tightened for volatile names.

    Only ever tightens: the configured cap stays a hard ceiling, so this cannot
    be used to justify a larger position than the risk settings permit.
    """
    return min(base_cap_pct, base_cap_pct * scale_for(vol_pct))


def summarize(bars: dict[str, list[dict]], symbols: list[str], capital: float,
              target_positions: int, held: set[str] | None = None) -> str:
    """Context block giving the AI a per-name suggested size."""
    v = vols(bars, symbols)
    if not v:
        return ""
    held = held or set()
    base = capital / max(1, target_positions)

    ranked = sorted(v.items(), key=lambda kv: kv[1])
    lines = [
        "",
        f"VOLATILITY-ADJUSTED SIZING (full-weight is ~${base:,.0f}, adjusted for "
        f"how much each name actually moves):",
    ]
    for sym, vol in ranked:
        dollars = target_dollars(capital, target_positions, vol)
        mark = " ●" if sym in held else ""
        note = ("calmer than average - can carry a larger position"
                if dollars > base * 1.05 else
                "more volatile - size down" if dollars < base * 0.95 else
                "about average")
        lines.append(f"  {sym:<6} vol {vol:5.1f}%/yr → ~${dollars:6,.0f}  ({note}){mark}")

    missing = [s for s in symbols if s not in v]
    if missing:
        lines.append(f"  No volatility data (use the plain ~${base:,.0f}): "
                     f"{', '.join(missing[:12])}"
                     + (f" +{len(missing) - 12} more" if len(missing) > 12 else ""))
    lines.append(
        "  Size to EQUAL RISK, not equal dollars: a wild name and a calm name at the "
        "same dollar size are not the same bet. ● marks names you already hold."
    )
    return "\n".join(lines)
