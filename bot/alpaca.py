"""A small, dependency-light Alpaca REST client.

Covers just what the bot needs: account, positions, orders, the market
clock (all on the trading host), plus bars, latest quotes and news (on the
data host). Uses `requests` directly so the whole surface is transparent and
easy to audit - this code can move real money in live mode.
"""
from __future__ import annotations

from typing import Any

import requests

from .config import DATA_HOST, Config


class AlpacaError(RuntimeError):
    pass


class Alpaca:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": cfg.alpaca_api_key,
                "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
                "accept": "application/json",
            }
        )

    # ---- low level -------------------------------------------------------
    def _get(self, host: str, path: str, params: dict | None = None) -> Any:
        r = self.session.get(f"{host}{path}", params=params, timeout=30)
        if r.status_code >= 400:
            raise AlpacaError(f"GET {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def _post(self, host: str, path: str, body: dict) -> Any:
        r = self.session.post(f"{host}{path}", json=body, timeout=30)
        if r.status_code >= 400:
            raise AlpacaError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    # ---- trading host ----------------------------------------------------
    def account(self) -> dict:
        return self._get(self.cfg.trading_host, "/v2/account")

    def positions(self) -> list[dict]:
        return self._get(self.cfg.trading_host, "/v2/positions")

    def orders(self, status: str = "all", limit: int = 50) -> list[dict]:
        return self._get(
            self.cfg.trading_host,
            "/v2/orders",
            {"status": status, "limit": limit, "direction": "desc"},
        )

    def clock(self) -> dict:
        return self._get(self.cfg.trading_host, "/v2/clock")

    def asset(self, symbol: str) -> dict:
        return self._get(self.cfg.trading_host, f"/v2/assets/{symbol}")

    def submit_order(
        self,
        symbol: str,
        side: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> dict:
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if notional is not None:
            body["notional"] = round(float(notional), 2)
        if qty is not None:
            body["qty"] = str(qty)
        return self._post(self.cfg.trading_host, "/v2/orders", body)

    # ---- data host (IEX feed works on Alpaca's free plan) ----------------
    def daily_bars(self, symbols: list[str], limit: int = 30) -> dict[str, list[dict]]:
        if not symbols:
            return {}
        data = self._get(
            DATA_HOST,
            "/v2/stocks/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "limit": limit,
                "feed": "iex",
                "adjustment": "raw",
            },
        )
        return data.get("bars", {}) or {}

    def latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        data = self._get(
            DATA_HOST,
            "/v2/stocks/quotes/latest",
            {"symbols": ",".join(symbols), "feed": "iex"},
        )
        return data.get("quotes", {}) or {}

    def news(self, symbols: list[str], limit: int = 20) -> list[dict]:
        if not symbols:
            return []
        data = self._get(
            DATA_HOST,
            "/v1beta1/news",
            {"symbols": ",".join(symbols), "limit": limit, "sort": "desc"},
        )
        return data.get("news", []) or []
