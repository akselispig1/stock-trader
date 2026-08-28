"""Configuration for the AI stockbroker, read from environment variables.

Locally, values come from a `.env` file (see `.env.example`). In GitHub
Actions they come from repository Secrets and Variables. Nothing here is
hardcoded and no secret is ever written to disk.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()  # no-op if there's no .env (e.g. in CI)
except ImportError:  # python-dotenv is optional at runtime
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    return int(_float(name, default))


def _opt_float(name: str, default: float | None) -> float | None:
    """Like _float, but a value of 0 (or negative) means 'disabled' -> None."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else None


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# Risk presets. RISK_LEVEL picks one; individual MAX_/MIN_ env vars still override
# the chosen preset. None of them enable shorting (that stays behind ALLOW_SHORT).
# RISK_LEVEL controls AGGRESSIVENESS (how fully invested, how many names, how
# many trades per cycle) - NOT concentration. The per-symbol cap stays modest at
# every level so the book stays a diversified set of medium/small positions
# rather than a few huge bets. Raise MAX_ALLOCATION_PCT_PER_SYMBOL explicitly if
# you ever want a concentrated book.
RISK_PRESETS: dict[str, dict[str, float]] = {
    "low":       {"alloc": 15.0, "reserve": 20.0, "orders": 3, "positions": 6},
    "medium":    {"alloc": 18.0, "reserve": 10.0, "orders": 4, "positions": 8},
    "semi-high": {"alloc": 20.0, "reserve": 5.0,  "orders": 5, "positions": 10},
    "high":      {"alloc": 22.0, "reserve": 0.0,  "orders": 6, "positions": 12},
}


def _norm_level(raw: str) -> str:
    lvl = (raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {"semihigh": "semi-high", "semi": "semi-high", "mid": "medium", "med": "medium"}
    lvl = aliases.get(lvl, lvl)
    return lvl if lvl in RISK_PRESETS else "medium"


def _risk_level() -> str:
    return _norm_level(os.getenv("RISK_LEVEL", "medium"))


def _risk(key: str, env_name: str) -> float:
    """Preset value for the active RISK_LEVEL, overridable by an explicit env var."""
    return _float(env_name, RISK_PRESETS[_risk_level()][key])


# Alpaca hosts. Paper and live share the data host; only the trading host differs.
PAPER_TRADING_HOST = "https://paper-api.alpaca.markets"
LIVE_TRADING_HOST = "https://api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"


@dataclass
class Config:
    # --- Credentials ---
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))

    # --- Mode & safety ---
    trading_mode: str = field(default_factory=lambda: os.getenv("TRADING_MODE", "paper").strip().lower())
    live_require_approval: bool = field(default_factory=lambda: _bool("LIVE_REQUIRE_APPROVAL", True))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))

    # --- The AI ---
    model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-5").strip())
    enable_web_search: bool = field(default_factory=lambda: _bool("ENABLE_WEB_SEARCH", True))
    # A second, independent AI that audits the trader AI's decisions before they
    # execute and can veto trades that aren't clearly justified (anti-black-box).
    enable_auditor: bool = field(default_factory=lambda: _bool("ENABLE_AUDITOR", True))
    # Cheap gate: a small model decides whether a full research cycle is worth
    # running this tick, skipping quiet cycles to save cost.
    triage_enabled: bool = field(default_factory=lambda: _bool("TRIAGE_ENABLED", True))
    triage_model: str = field(default_factory=lambda: os.getenv("TRIAGE_MODEL", "claude-haiku-4-5").strip())

    # --- Always-on runner (bot.serve) ---
    # How often to run a cycle while the market is open, and how often to
    # re-check the clock while it is closed. Only used by bot.serve; the
    # GitHub cron has its own schedule.
    cycle_minutes: float = field(default_factory=lambda: _float("CYCLE_MINUTES", 30.0))
    closed_poll_minutes: float = field(default_factory=lambda: _float("CLOSED_POLL_MINUTES", 30.0))

    # --- Universe ---
    # Diversified across sectors + ETFs so the bot isn't trapped in correlated
    # mega-cap AI: tech, financials, healthcare, energy, staples, plus broad and
    # sector/asset ETFs. Override with the WATCHLIST env/var.
    watchlist: list[str] = field(
        default_factory=lambda: _list(
            "WATCHLIST",
            [
                # mega-cap tech
                "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
                # financials / healthcare / energy / staples / industrial
                "JPM", "V", "UNH", "JNJ", "XOM", "WMT", "COST", "CAT",
                # ETFs: broad, small-cap, gold, energy, financials
                "SPY", "QQQ", "IWM", "GLD", "XLE", "XLF",
            ],
        )
    )

    # --- Fundamental "value scout" ---
    # A cached (refreshed ~daily) AI scan that judges each name's price vs. its
    # latest-quarter fundamentals, flagging undervalued (price below fair value)
    # buy candidates and price-already-reflects-it names to skip.
    enable_fundamentals: bool = field(default_factory=lambda: _bool("ENABLE_FUNDAMENTALS", True))
    fundamentals_ttl_hours: float = field(default_factory=lambda: _float("FUNDAMENTALS_TTL_HOURS", 20.0))

    # --- Stop-loss (AI-reviewed) ---
    # A position down more than STOP_LOSS_REVIEW_PCT is flagged to the trader AI
    # to decide cut vs. hold with reasoning. STOP_LOSS_HARD_PCT (0 = off) is a
    # dumb safety backstop that force-closes catastrophic losers regardless.
    stop_loss_review_pct: float = field(default_factory=lambda: _float("STOP_LOSS_REVIEW_PCT", 8.0))
    stop_loss_hard_pct: float = field(default_factory=lambda: _float("STOP_LOSS_HARD_PCT", 0.0))

    # --- Capital cap ---
    # The bot behaves as if it only has this much money, regardless of the real
    # (much larger) paper balance. Sizing and risk are computed against it. Set
    # CAPITAL_CAP=0 to disable and use the full account. Currency is a label only
    # (the Alpaca account is USD-denominated).
    capital_cap: float | None = field(default_factory=lambda: _opt_float("CAPITAL_CAP", 1000.0))
    capital_currency: str = field(default_factory=lambda: os.getenv("CAPITAL_CURRENCY", "CHF").strip() or "CHF")

    # --- Risk limits ---
    # RISK_LEVEL (low | medium | semi-high | high) sets the preset; the specific
    # MAX_/MIN_ vars below still override it if set explicitly.
    risk_level: str = field(default_factory=_risk_level)
    max_orders_per_run: int = field(default_factory=lambda: int(_risk("orders", "MAX_ORDERS_PER_RUN")))
    # Roughly how many positions the book should hold - drives position sizing
    # (target weight ~ 100/target_positions %). Many medium/small positions.
    target_positions: int = field(default_factory=lambda: int(_risk("positions", "TARGET_POSITIONS")))
    max_notional_per_order: float = field(default_factory=lambda: _float("MAX_NOTIONAL_PER_ORDER", 1000.0))
    max_allocation_pct_per_symbol: float = field(
        default_factory=lambda: _risk("alloc", "MAX_ALLOCATION_PCT_PER_SYMBOL")
    )
    min_cash_reserve_pct: float = field(default_factory=lambda: _risk("reserve", "MIN_CASH_RESERVE_PCT"))
    allow_short: bool = field(default_factory=lambda: _bool("ALLOW_SHORT", False))

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def trading_host(self) -> str:
        return LIVE_TRADING_HOST if self.is_live else PAPER_TRADING_HOST

    def validate(self) -> None:
        missing = []
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not self.alpaca_api_key:
            missing.append("ALPACA_API_KEY")
        if not self.alpaca_secret_key:
            missing.append("ALPACA_SECRET_KEY")
        if missing:
            raise SystemExit(
                "Missing required credentials: "
                + ", ".join(missing)
                + "\nSet them in a local .env file (see .env.example) or as GitHub "
                "Actions secrets."
            )
        if self.trading_mode not in ("paper", "live"):
            raise SystemExit(f"TRADING_MODE must be 'paper' or 'live', got '{self.trading_mode}'")

    def public_summary(self) -> dict:
        """Non-secret config, safe to write into the dashboard state file."""
        return {
            "trading_mode": self.trading_mode,
            "model": self.model,
            "enable_web_search": self.enable_web_search,
            "watchlist": self.watchlist,
            "dry_run": self.dry_run,
            "live_require_approval": self.live_require_approval,
            "capital_cap": self.capital_cap,
            "capital_currency": self.capital_currency,
            "enable_auditor": self.enable_auditor,
            "triage_enabled": self.triage_enabled,
            "triage_model": self.triage_model,
            "enable_fundamentals": self.enable_fundamentals,
            "stop_loss_review_pct": self.stop_loss_review_pct,
            "stop_loss_hard_pct": self.stop_loss_hard_pct,
            "risk": {
                "level": self.risk_level,
                "max_orders_per_run": self.max_orders_per_run,
                "target_positions": self.target_positions,
                "max_notional_per_order": self.max_notional_per_order,
                "max_allocation_pct_per_symbol": self.max_allocation_pct_per_symbol,
                "min_cash_reserve_pct": self.min_cash_reserve_pct,
                "allow_short": self.allow_short,
            },
        }
