"""Orchestration: gather -> research -> decide -> risk-check -> execute -> record.

This is the heart of the bot. It is deliberately conservative: Claude proposes
trades, but every order is validated against the hard risk limits in `Config`
here, in plain Python, before anything is sent to Alpaca. Claude cannot exceed
those limits no matter what it returns.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .alpaca import Alpaca, AlpacaError
from .auditor import Auditor
from .brain import Brain
from .config import Config

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
LEDGER_PATH = os.path.join(DATA_DIR, "ledger.json")


def _load_ledger() -> dict:
    """Persistent P&L/cost ledger: the equity baseline the book's P&L is measured
    from, plus the running total of AI operating cost."""
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"baseline_equity": None, "cumulative_ai_cost": 0.0}


def _save_ledger(ledger: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _pct(bars: list[dict]) -> float | None:
    """Percent change from the first to the last close in a bar series."""
    if not bars or len(bars) < 2:
        return None
    first, last = _f(bars[0].get("c")), _f(bars[-1].get("c"))
    if first == 0:
        return None
    return (last - first) / first * 100.0


def _add_detail(o: dict, msg: str) -> None:
    """Append a human-readable note to an order's `detail` field."""
    o["detail"] = f"{o['detail']}; {msg}" if o.get("detail") else msg


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.alpaca = Alpaca(cfg)
        self.brain = Brain(cfg)
        self.auditor = Auditor(cfg)

    # ---- capital cap -----------------------------------------------------
    def effective_funds(self, account: dict, positions: list[dict]) -> tuple[float, float, float]:
        """Return (equity, cash, invested) the bot should size against.

        With a capital cap the bot behaves as if it holds only `capital_cap`,
        no matter how large the real paper balance is: equity is the cap,
        available cash is the cap minus what's already invested, and neither
        exceeds the real account's actual figures.
        """
        real_equity = _f(account.get("equity"))
        real_cash = _f(account.get("cash"))
        invested_real = sum(abs(_f(p.get("market_value"))) for p in positions)
        cap = self.cfg.capital_cap
        if cap is None:
            return real_equity, real_cash, invested_real
        eff_equity = min(real_equity, cap)
        invested = min(invested_real, cap)
        eff_cash = max(0.0, min(real_cash, cap - invested))
        return eff_equity, eff_cash, invested

    # ---- context building -----------------------------------------------
    def build_context(self) -> tuple[str, dict, list[dict]]:
        account = self.alpaca.account()
        positions = self.alpaca.positions()

        equity, cash, invested = self.effective_funds(account, positions)
        cap = self.cfg.capital_cap

        lines = [
            f"MODE: {self.cfg.trading_mode.upper()} (fake money)"
            if not self.cfg.is_live
            else "MODE: LIVE (REAL money)",
        ]
        if cap is not None:
            lines += [
                f"MANAGED BUDGET: {cap:,.0f} {self.cfg.capital_currency} — you must keep "
                f"TOTAL invested within this budget. Size every position accordingly.",
                f"Invested so far: ${invested:,.2f} | Available to invest: ${cash:,.2f}",
                f"(Underlying paper account equity is ${_f(account.get('equity')):,.2f}, "
                f"but treat {cap:,.0f} {self.cfg.capital_currency} as your entire capital.)",
            ]
        else:
            lines += [
                f"Account equity: ${equity:,.2f}",
                f"Cash: ${cash:,.2f} | Buying power: ${_f(account.get('buying_power')):,.2f}",
            ]
        lines += ["", "CURRENT POSITIONS:"]
        if positions:
            for p in positions:
                lines.append(
                    f"  {p['symbol']}: {p.get('qty')} sh, "
                    f"mkt_value ${_f(p.get('market_value')):,.2f}, "
                    f"unrealized P&L ${_f(p.get('unrealized_pl')):,.2f} "
                    f"({_f(p.get('unrealized_plpc')) * 100:.1f}%)"
                )
        else:
            lines.append("  (none - all cash)")

        # Market data for the watchlist (best-effort; data API may be limited on
        # a brand-new account, so never let it abort the run).
        symbols = self.cfg.watchlist
        try:
            bars = self.alpaca.daily_bars(symbols, limit=20)
        except AlpacaError:
            bars = {}
        try:
            news = self.alpaca.news(symbols, limit=15)
        except AlpacaError:
            news = []

        lines += ["", "WATCHLIST (recent daily price action):"]
        for sym in symbols:
            b = bars.get(sym) or []
            if b:
                last = _f(b[-1].get("c"))
                chg = _pct(b)
                chg_s = f"{chg:+.1f}% over {len(b)}d" if chg is not None else "n/a"
                lines.append(f"  {sym}: last ${last:,.2f} ({chg_s})")
            else:
                lines.append(f"  {sym}: (no recent data)")

        if news:
            lines += ["", "RECENT HEADLINES:"]
            for n in news[:15]:
                syms = ",".join(n.get("symbols", [])[:3])
                lines.append(f"  [{syms}] {n.get('headline', '').strip()}")

        return "\n".join(lines), account, positions

    # ---- risk checks -----------------------------------------------------
    def risk_check(
        self, orders: list[dict], account: dict, positions: list[dict]
    ) -> list[dict]:
        # Size against the capital cap (if set), not the full paper balance.
        equity, cash, _invested = self.effective_funds(account, positions)
        pos_by_sym = {p["symbol"]: p for p in positions}
        cash_floor = equity * (self.cfg.min_cash_reserve_pct / 100.0)
        max_alloc = equity * (self.cfg.max_allocation_pct_per_symbol / 100.0)

        # Running state, updated as each order is APPROVED within this run, so
        # multiple orders in the same cycle can't collectively breach a limit.
        # `exposure` starts from the current position market values. Cash is
        # tracked conservatively: buys reduce it, but sell proceeds are NOT
        # credited toward same-run buys (a sell may end up deferred, or only
        # human-approved in live mode, so its cash isn't guaranteed to arrive).
        exposure = {
            sym: abs(_f(p.get("market_value"))) for sym, p in pos_by_sym.items()
        }
        projected_cash = cash
        approved_count = 0

        checked: list[dict] = []
        for order in orders:
            symbol = str(order.get("symbol", "")).upper()
            side = str(order.get("side", "")).lower()
            notional = round(abs(_f(order.get("notional_usd"))), 2)
            o = {
                "symbol": symbol,
                "side": side,
                "notional_usd": notional,
                "reasoning": order.get("reasoning", ""),
                "confidence": _f(order.get("confidence")),
                "status": "pending",
                "detail": "",
            }

            def reject(msg: str):
                o["status"] = "rejected"
                o["detail"] = msg
                checked.append(o)

            if side not in ("buy", "sell"):
                reject(f"invalid side '{side}'")
                continue
            if notional <= 0:
                reject("non-positive notional")
                continue
            if side == "buy" and symbol not in self.cfg.watchlist:
                reject("buy not on watchlist")
                continue
            # Count only orders that make it through validation toward the cap,
            # so a batch of rejects can't crowd out valid trades.
            if approved_count >= self.cfg.max_orders_per_run:
                reject(f"max {self.cfg.max_orders_per_run} orders/run reached")
                continue
            if notional > self.cfg.max_notional_per_order:
                notional = self.cfg.max_notional_per_order
                o["notional_usd"] = notional
                _add_detail(o, f"capped to ${notional:,.0f} per-order limit")

            if side == "buy":
                held = exposure.get(symbol, 0.0)
                if held + notional > max_alloc:
                    room = max(0.0, max_alloc - held)
                    if room < 1:
                        reject(f"{symbol} already at {self.cfg.max_allocation_pct_per_symbol:.0f}% cap")
                        continue
                    notional = round(room, 2)
                    o["notional_usd"] = notional
                    _add_detail(o, f"trimmed to allocation cap (${notional:,.0f})")
                if projected_cash - notional < cash_floor:
                    reject(
                        f"would breach ${cash_floor:,.0f} cash reserve "
                        f"(cash left ${projected_cash:,.0f})"
                    )
                    continue
                projected_cash -= notional
                exposure[symbol] = held + notional
            elif symbol in pos_by_sym:  # sell an existing long position
                held_val = abs(_f(pos_by_sym[symbol].get("market_value")))
                held_qty = abs(_f(pos_by_sym[symbol].get("qty")))
                if notional >= held_val:
                    # Full exit: close by exact share qty so price drift between
                    # now and fill can't leave a dust position behind.
                    notional = round(held_val, 2)
                    o["notional_usd"] = notional
                    o["qty"] = held_qty
                    _add_detail(o, "closing full position")
                exposure[symbol] = max(0.0, exposure.get(symbol, 0.0) - notional)
            else:  # sell with no position held
                if not self.cfg.allow_short:
                    reject("sell but no position held (shorting disabled)")
                    continue
                # Short: apply the same per-symbol allocation cap to short size.
                held = exposure.get(symbol, 0.0)
                if held + notional > max_alloc:
                    room = max(0.0, max_alloc - held)
                    if room < 1:
                        reject(f"{symbol} short already at allocation cap")
                        continue
                    notional = round(room, 2)
                    o["notional_usd"] = notional
                    _add_detail(o, f"short trimmed to allocation cap (${notional:,.0f})")
                exposure[symbol] = held + notional

            o["status"] = "approved"
            approved_count += 1
            checked.append(o)
        return checked

    # ---- independent audit ----------------------------------------------
    def run_audit(self, memo: str, context: str, checked: list[dict]) -> dict | None:
        """Have the second AI review the approved orders; veto the unjustified.

        Mutates `checked` in place (approved -> vetoed where the auditor objects)
        and returns the audit record for the dashboard, or None if disabled/no
        orders. Fails open on error: the hard risk limits already applied, so a
        transient auditor outage should not silently block a validated trade.
        """
        to_audit = [o for o in checked if o["status"] == "approved"]
        if not self.cfg.enable_auditor or not to_audit:
            return None
        try:
            audit = self.auditor.audit(memo, context, to_audit)
        except Exception as e:  # fail open, but record that the audit is missing
            print(f"[engine] auditor unavailable: {e}")
            return {"verdict": "unavailable", "transparency_score": None,
                    "summary": f"Independent audit could not run this cycle ({e}).",
                    "order_reviews": []}

        reviews = {r.get("index"): r for r in audit.get("order_reviews", [])}
        reject_all = audit.get("verdict") == "reject"
        vetoed = 0
        for i, o in enumerate(to_audit):
            r = reviews.get(i)
            if reject_all or (r and r.get("decision") == "veto"):
                o["status"] = "vetoed"
                reason = (r.get("reason") if r else None) or "auditor rejected the cycle"
                _add_detail(o, f"auditor veto: {reason}")
                vetoed += 1
        print(f"[engine] auditor verdict={audit.get('verdict')} "
              f"transparency={audit.get('transparency_score')} vetoed={vetoed}")
        return audit

    # ---- execution -------------------------------------------------------
    def execute(self, checked: list[dict], market_open: bool, force: bool) -> None:
        # In live mode with approval required, we never auto-send: orders are
        # left as 'proposed' for the human to approve from the dashboard.
        live_hold = self.cfg.is_live and self.cfg.live_require_approval

        for o in checked:
            if o["status"] != "approved":
                continue
            if self.cfg.dry_run:
                o["status"] = "dry_run"
                _add_detail(o, "dry run - not sent")
                continue
            if live_hold:
                o["status"] = "proposed"
                _add_detail(o, "awaiting approval (live)")
                continue
            if not market_open and not force:
                o["status"] = "deferred"
                _add_detail(o, "market closed - not sent")
                continue
            try:
                if o.get("qty"):  # full-position exit: send exact share qty
                    res = self.alpaca.submit_order(o["symbol"], o["side"], qty=o["qty"])
                else:
                    res = self.alpaca.submit_order(
                        o["symbol"], o["side"], notional=o["notional_usd"]
                    )
                o["status"] = "executed"
                o["order_id"] = res.get("id")
                _add_detail(o, f"submitted ({res.get('status', 'accepted')})")
            except AlpacaError as e:
                o["status"] = "error"
                o["detail"] = str(e)[:200]

    # ---- state files -----------------------------------------------------
    def write_state(
        self, account: dict, positions: list[dict], memo: str, plan: dict,
        checked: list[dict], market_open: bool, audit: dict | None = None,
        ledger: dict | None = None, cycle_cost: float = 0.0,
    ) -> dict:
        os.makedirs(DATA_DIR, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        equity = _f(account.get("equity"))
        eff_equity, eff_cash, invested = self.effective_funds(account, positions)

        # True book P&L: this bot is the only thing trading the account, so the
        # account's equity change from the ledger baseline IS the book's gross
        # gain/loss (realised + unrealised). Net subtracts cumulative AI cost.
        ledger = ledger or _load_ledger()
        baseline = _f(ledger.get("baseline_equity")) or equity
        ai_cost_total = _f(ledger.get("cumulative_ai_cost"))
        gross_pnl = equity - baseline
        net_pnl = gross_pnl - ai_cost_total
        base = self.cfg.capital_cap if self.cfg.capital_cap is not None else baseline
        # book_equity reflects real P&L (not clamped to the cap); net is after cost.
        book_equity = base + gross_pnl
        net_book_equity = base + net_pnl

        state = {
            "updated_at": now,
            "config": self.cfg.public_summary(),
            "market_open": market_open,
            "account": {
                "equity": equity,
                "cash": _f(account.get("cash")),
                "buying_power": _f(account.get("buying_power")),
                "portfolio_value": _f(account.get("portfolio_value")),
                "last_equity": _f(account.get("last_equity")),
                "capital_cap": self.cfg.capital_cap,
                "capital_currency": self.cfg.capital_currency,
                "effective_equity": eff_equity,
                "effective_cash": eff_cash,
                "invested": invested,
                "book_equity": book_equity,
                "net_book_equity": net_book_equity,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "ai_cost_cycle": cycle_cost,
                "ai_cost_total": ai_cost_total,
            },
            "positions": [
                {
                    "symbol": p["symbol"],
                    "qty": _f(p.get("qty")),
                    "market_value": _f(p.get("market_value")),
                    "avg_entry_price": _f(p.get("avg_entry_price")),
                    "current_price": _f(p.get("current_price")),
                    "unrealized_pl": _f(p.get("unrealized_pl")),
                    "unrealized_plpc": _f(p.get("unrealized_plpc")),
                }
                for p in positions
            ],
            "market_summary": plan.get("market_summary", ""),
            "memo": memo,
            "orders": checked,
            "audit": audit,
        }
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)

        history_row = {
            "t": now,
            "equity": book_equity,          # gross book value (P&L vs baseline)
            "net_equity": net_book_equity,  # after cumulative AI cost
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "ai_cost_total": ai_cost_total,
            "cash": eff_cash,
            "n_orders": sum(1 for o in checked if o["status"] in ("executed", "proposed", "dry_run")),
            "summary": plan.get("market_summary", "")[:280],
        }
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(history_row) + "\n")

        return state

    # ---- top-level run ---------------------------------------------------
    def run(self, force: bool = False) -> dict:
        self.cfg.validate()
        try:
            clock = self.alpaca.clock()
            market_open = bool(clock.get("is_open"))
        except AlpacaError:
            market_open = False

        context, account, positions = self.build_context()
        print(f"[engine] equity=${_f(account.get('equity')):,.2f} "
              f"positions={len(positions)} market_open={market_open}")

        memo = self.brain.research(context)
        print("[engine] research memo received "
              f"({len(memo)} chars)")

        plan = self.brain.structure(memo, context)
        raw_orders = plan.get("orders", [])
        print(f"[engine] AI proposed {len(raw_orders)} order(s)")

        checked = self.risk_check(raw_orders, account, positions)

        # Independent second AI reviews what would actually be traded and can
        # veto orders that aren't honestly justified (the anti-black-box gate).
        self.auditor.last_cost = 0.0
        audit = self.run_audit(memo, context, checked)

        self.execute(checked, market_open, force)
        for o in checked:
            print(f"  - {o['side']} {o['symbol']} ${o['notional_usd']:,.0f} "
                  f"-> {o['status']} {('(' + o['detail'] + ')') if o['detail'] else ''}")

        # If we placed anything, refresh so the dashboard shows post-trade
        # balances and the new positions immediately (not one cycle late).
        if any(o["status"] == "executed" for o in checked):
            try:
                account = self.alpaca.account()
                positions = self.alpaca.positions()
            except AlpacaError:
                pass

        # AI operating cost for this cycle, and the running P&L-vs-cost ledger.
        cycle_cost = self.brain.run_cost + self.auditor.last_cost
        ledger = _load_ledger()
        if ledger.get("baseline_equity") in (None, 0):
            ledger["baseline_equity"] = _f(account.get("equity"))
        ledger["cumulative_ai_cost"] = _f(ledger.get("cumulative_ai_cost")) + cycle_cost
        _save_ledger(ledger)
        print(f"[engine] AI cost this cycle=${cycle_cost:.4f} "
              f"total=${_f(ledger['cumulative_ai_cost']):.2f}")

        state = self.write_state(account, positions, memo, plan, checked,
                                 market_open, audit, ledger, cycle_cost)
        print("[engine] wrote docs/data/state.json")
        return state
