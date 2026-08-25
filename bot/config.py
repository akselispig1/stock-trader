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
    model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5").strip())
    enable_web_search: bool = field(default_factory=lambda: _bool("ENABLE_WEB_SEARCH", True))

    # --- Universe ---
    watchlist: list[str] = field(
        default_factory=lambda: _list(
            "WATCHLIST",
            ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD", "SPY", "QQQ"],
        )
    )

    # --- Capital cap ---
    # The bot behaves as if it only has this much money, regardless of the real
    # (much larger) paper balance. Sizing and risk are computed against it. Set
    # CAPITAL_CAP=0 to disable and use the full account. Currency is a label only
    # (the Alpaca account is USD-denominated).
    capital_cap: float | None = field(default_factory=lambda: _opt_float("CAPITAL_CAP", 1000.0))
    capital_currency: str = field(default_factory=lambda: os.getenv("CAPITAL_CURRENCY", "CHF").strip() or "CHF")

    # --- Risk limits ---
    max_orders_per_run: int = field(default_factory=lambda: _int("MAX_ORDERS_PER_RUN", 4))
    max_notional_per_order: float = field(default_factory=lambda: _float("MAX_NOTIONAL_PER_ORDER", 1000.0))
    max_allocation_pct_per_symbol: float = field(
        default_factory=lambda: _float("MAX_ALLOCATION_PCT_PER_SYMBOL", 25.0)
    )
    min_cash_reserve_pct: float = field(default_factory=lambda: _float("MIN_CASH_RESERVE_PCT", 5.0))
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
            "risk": {
                "max_orders_per_run": self.max_orders_per_run,
                "max_notional_per_order": self.max_notional_per_order,
                "max_allocation_pct_per_symbol": self.max_allocation_pct_per_symbol,
                "min_cash_reserve_pct": self.min_cash_reserve_pct,
                "allow_short": self.allow_short,
            },
        }
