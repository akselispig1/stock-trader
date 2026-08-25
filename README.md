# 🤖📈 AI Stockbroker

An autonomous AI stockbroker. **Claude** researches the market (live news, web
search, and price data), decides what to trade, and places the trades through
your **Alpaca** brokerage account — while a clean web **dashboard** shows you
everything it's doing.

It runs entirely on GitHub, free:

- **The brain** is a scheduled **GitHub Action** that runs a research → decide →
  trade cycle during market hours (and on demand). This is what makes it a
  hands-off, "runs by itself" bot — and the piece you'll later point at an
  always-on host for true 24/7.
- **The dashboard** is a **GitHub Pages** website — the link you open in a
  browser to watch the bot work, see its reasoning, and (in live mode) approve
  trades.

> ### ⚠️ Read this first
> This is an educational project, **not financial advice**. It starts in
> **paper mode** — Alpaca's sandbox that uses **fake money but real, live market
> data**, so you can test the AI's research and execution with zero financial
> risk. It will **never touch real money** until you deliberately switch it to
> live mode. Even then, keep only money you can afford to lose in the account.

---

## How it works

```
                 ┌──────────────── GitHub Actions (the brain) ────────────────┐
                 │  every 30 min in market hours, or on demand                 │
   Alpaca  ─────►│  1. read account + positions + watchlist prices + news      │
   (paper)       │  2. Claude researches (web search) & writes a memo          │
                 │  3. Claude emits a structured trade plan                    │
                 │  4. Python enforces risk limits, then places paper orders   │
                 │  5. writes docs/data/state.json  ──commits to repo──┐       │
                 └─────────────────────────────────────────────────────┼──────┘
                                                                        ▼
   Your browser ─────►  GitHub Pages dashboard  ◄── reads state.json + history
                        (portfolio, the AI's reasoning, trades, live view)
```

Two things power it, and you supply the keys for both:

| Piece | What it does | You need |
|------|---------------|----------|
| **Claude** (Anthropic) | The actual "AI" — research and decisions | An Anthropic API key ✅ *(you have this)* |
| **Alpaca** | The brokerage that holds the money and executes | Free Alpaca **paper** API keys |

---

## Setup (about 10 minutes)

### 1. Get your Alpaca paper keys (free, instant)

1. Sign up at **[alpaca.markets](https://alpaca.markets)**.
2. In the dashboard, switch to **Paper Trading** (top-left toggle).
3. Go to **Home → API Keys → Generate** (make sure you're in *paper* mode).
4. Copy the **Key ID** and **Secret Key** (the secret is shown only once).

Your paper account starts with $100,000 of fake money.

### 2. Add your keys to the repo as Secrets

In this repository: **Settings → Secrets and variables → Actions → Secrets → New repository secret.** Add three:

| Secret name | Value |
|-------------|-------|
| `ANTHROPIC_API_KEY` | your Anthropic key (`sk-ant-…`) |
| `ALPACA_API_KEY` | your Alpaca paper Key ID |
| `ALPACA_SECRET_KEY` | your Alpaca paper Secret Key |

Secrets are encrypted and never visible in logs or the dashboard.

### 3. Turn on the dashboard (GitHub Pages)

Once this branch is merged to `main`: **Settings → Pages → Source: “Deploy from a
branch” → Branch: `main` / folder: `/docs` → Save.**

After a minute your dashboard is live at:

```
https://<your-username>.github.io/<repo-name>/
```

That's your browser link. 🎉 (It shows a "no run yet" placeholder until the first
bot cycle runs.)

### 4. Do your first test run

Go to the **Actions** tab → **AI Stockbroker** → **Run workflow**. Options:

- **Research only** ✅ — for the very first run, tick this. The AI does full
  research and builds a trade plan but places **no** orders, so you can read its
  thinking safely.
- Leave both off for a normal run: in paper mode it will actually place the
  fake-money trades so you can watch execution end to end.

When it finishes, refresh your dashboard — you'll see the AI's market summary,
its full research memo, the orders, and your updated paper portfolio.

After that it runs **automatically** every 30 minutes during US market hours.

---

## Trying it locally (optional)

```bash
pip install -r requirements.txt
cp .env.example .env      # then paste your keys into .env
python -m bot.run --dry-run   # research + plan, no orders
python -m bot.run             # a real paper cycle
```

`.env` is git-ignored, so your keys stay on your machine.

---

## The dashboard

Open your Pages link to get:

- **Portfolio** — equity, cash, day P&L, buying power, and an equity curve that
  grows with every run.
- **Latest AI decision** — the market summary, the full research memo, and every
  order with its status and the AI's reasoning.
- **Positions** — what the bot holds and the P&L on each.
- **Ask the AI now** — type a ticker and Claude researches it live *in your
  browser* (with web search). Read-only; great for testing its research on
  demand. Your Anthropic key is stored only in your browser.
- **Live account view** — optionally paste your Alpaca keys to see real-time
  balances, and (in live mode) approve the bot's proposed trades with one click.
  Keys never leave your browser.

---

## Going live (real money) — later, deliberately

When you're ready to trade real money:

1. Generate **live** Alpaca keys (Alpaca dashboard in *Live* mode) and fund the
   account, then update the `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` secrets.
2. Set a repo **Variable** (Settings → Secrets and variables → Actions →
   **Variables**): `TRADING_MODE = live`.
3. By default, live mode is **approval-only**: the bot researches and *proposes*
   trades but does not send them. You approve each one from the dashboard's
   **Live account view**. This is the recommended way to start.
4. Only once you fully trust it, set `LIVE_REQUIRE_APPROVAL = false` to let it
   trade real money autonomously. ⚠️ Do this at your own risk.

---

## Tuning the bot

All optional — set these as repo **Variables** (not secrets). Defaults are in
`bot/config.py`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `TRADING_MODE` | `paper` | `paper` (sandbox) or `live` (real money) |
| `LIVE_REQUIRE_APPROVAL` | `true` | In live mode, require dashboard approval per trade |
| `CAPITAL_CAP` | `1000` | Bot behaves as if it holds only this much, whatever the real paper balance is. `0` = trade the full account |
| `CAPITAL_CURRENCY` | `CHF` | Label for the cap on the dashboard (the Alpaca account itself is USD) |
| `CLAUDE_MODEL` | `claude-opus-5` | The AI model. `claude-sonnet-5` is ~60% cheaper; `claude-haiku-4-5` cheapest (no web search) |
| `ENABLE_WEB_SEARCH` | `true` | Let the AI search the web for live news/sentiment |
| `WATCHLIST` | 10 large-caps | Comma-separated tickers the AI may trade, e.g. `AAPL,MSFT,SPY` |
| `MAX_ORDERS_PER_RUN` | `4` | Cap on trades per cycle |
| `MAX_NOTIONAL_PER_ORDER` | `1000` | Max dollars per single order |
| `MAX_ALLOCATION_PCT_PER_SYMBOL` | `25` | Max % of equity in any one symbol |
| `MIN_CASH_RESERVE_PCT` | `5` | Keep at least this % of equity in cash |
| `ALLOW_SHORT` | `false` | Allow short selling (off = only sell what you hold) |

**These risk limits are enforced in Python, after the AI decides** — Claude
physically cannot place an order that breaks them.

### Schedule

The cron in `.github/workflows/trader.yml` runs every 30 min, `13:30–20:00 UTC`
on weekdays, which covers US market hours during Eastern *Daylight* time. In
winter (EST) shift it an hour earlier if you want the full session. Note GitHub's
scheduled Actions can be delayed at busy times and pause after ~60 days of repo
inactivity — fine for experimenting, but for reliable **24/7** operation, run
`python -m bot.run` on a cheap always-on host (Render / Fly / Railway / a cron on
any VM) with the same environment variables. The dashboard keeps working exactly
the same.

---

## Project layout

```
bot/
  config.py     env-driven config + risk limits
  alpaca.py     Alpaca REST client (trading + market data)
  brain.py      Claude: research memo → structured trade plan
  engine.py     gather → research → decide → risk-check → execute → record
  run.py        CLI entry point (python -m bot.run)
docs/           the GitHub Pages dashboard (index.html, app.js, styles.css)
  data/         state.json + history.jsonl, written by the bot each run
.github/workflows/trader.yml   the scheduled/on-demand brain
```

## Safety notes

- Keys live in GitHub Secrets (server side) or your own browser's localStorage
  (dashboard tools) — never committed, never in logs, never in the page's data.
- The bot defaults to paper trading and to approval-gated live trading.
- Hard risk limits are enforced in code, independent of the AI.
- Not financial advice. Use at your own risk.
