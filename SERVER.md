# Running the bot on a dedicated laptop

Everything needed to turn a spare laptop into an always-on trading server: it
starts itself at boot, restarts if it crashes, survives the lid being closed,
and serves its dashboard to every device on your wifi.

---

## Before you start

**Keep it plugged in.** Every setup below disables sleep *on mains power only*.
On battery the machine will still suspend, and a suspended bot silently stops
trading — no error, no notification, just nothing happening.

**Wifi is fine, ethernet is better.** The bot survives a dropped connection
(it logs the failure and retries next cycle) but each outage during market
hours is a cycle it can't act on.

**Any laptop from the last decade is enough.** The bot is idle ~99% of the
time; a cycle is a few API calls and some JSON. 4 GB RAM and any CPU will do.
Claude does the thinking on Anthropic's servers, not on your machine.

---

## Which operating system?

| | Setup | Reliability | Pick this if |
|---|---|---|---|
| **Linux** (Ubuntu) | One command | Best — a real service, starts at boot even logged out | You're willing to install Ubuntu |
| **Windows** | One command | Good — starts at sign-in; needs auto-sign-in to be fully headless | The laptop already runs Windows |

Both are genuinely fine. Windows has one wrinkle (below) that Linux doesn't.

---

## Linux (Ubuntu or similar)

```bash
git clone https://github.com/akselispig1/stock-trader.git
cd stock-trader
./run-local.sh                      # creates .env, then stops
nano .env                           # paste your three API keys
./run-local.sh once                 # confirm one cycle works end to end
sudo ./deploy/install-linux.sh      # install it as a service
```

That last step:

- installs a **systemd service** that starts at boot and restarts 30s after any
  crash (giving up only if it fails 5 times in 10 minutes, so a genuinely
  broken install doesn't spin forever)
- **disables suspend, hibernate and lid-close sleep**
- starts it immediately and reports whether it came up

```bash
journalctl -u stocktrader -f            # follow the logs live
sudo systemctl stop|start stocktrader   # stop / start by hand
sudo ./deploy/uninstall-linux.sh        # undo everything
```

---

## Windows

Open PowerShell **as Administrator**:

```powershell
git clone https://github.com/akselispig1/stock-trader.git
cd stock-trader
.\run-local.bat                 # creates .env, then stops
notepad .env                    # paste your three API keys
.\run-local.bat once            # confirm one cycle works end to end
powershell -ExecutionPolicy Bypass -File .\deploy\install-windows.ps1
```

This registers a **scheduled task** that starts the bot at sign-in and restarts
it up to 5 times if it crashes, and disables sleep, hibernate and lid-close
suspend on mains power.

### The Windows wrinkle: it starts at sign-in, not at boot

A task that runs while logged *out* has to store your Windows password, which
is worse for security than it's worth here. So after a reboot or power cut, the
bot only starts once somebody signs in.

If the laptop is genuinely headless, enable automatic sign-in:

1. `Win+R` → `netplwiz` → Enter
2. Untick **"Users must enter a user name and password to use this computer"**
3. Apply, and enter the password when prompted

Now a reboot signs in on its own and the bot starts. **Only do this on a
machine nobody else can physically reach** — anyone who opens the lid is
already logged into your account.

```powershell
Stop-ScheduledTask  -TaskName "AI Stockbroker"
Start-ScheduledTask -TaskName "AI Stockbroker"
powershell -ExecutionPolicy Bypass -File .\deploy\uninstall-windows.ps1
```

---

## Checking it's actually working

**On the laptop:** <http://localhost:8080>

**From your phone or any device on the same wifi:** `http://<laptop-ip>:8080` —
the installer prints the address. The bot binds to `0.0.0.0`, so it's reachable
across your local network but *not* from the internet.

### `/healthz` is the real answer

<http://localhost:8080/healthz> returns JSON:

```json
{ "cycles": 14, "errors": 0, "healthy": true,
  "last_run": "2026-09-01T18:32:11Z", "last_run_age_s": 412,
  "uptime_s": 259200, "market_open": true, "last_error": null }
```

| Field | What it tells you |
|---|---|
| `healthy` | `true` only if at least one cycle has run **and** the last one didn't error |
| `last_run_age_s` | Seconds since the last completed cycle. During market hours this should stay under ~2× your `CYCLE_MINUTES` |
| `uptime_s` | Resets to 0 on every restart — a small number after days of running means it's crash-looping |
| `errors` | Cumulative. Slowly rising is normal (brief network blips); rapidly rising is not |

**A running process is not a working bot.** The loop deliberately survives
every error, so it will happily run for weeks with `cycles: 0` if your API key
is wrong. Check `healthy`, not whether the window is open.

### What to look at weekly

The **Versus the market** card on the dashboard. Net alpha is the only number
that says whether any of this beat leaving the money in an index fund. Absolute
profit in a rising market means very little.

The **What your risk level means** card tells you the drawdown to expect from
your current setting — check it once, before you get surprised by a dip that was
always in the range.

Then the **Track record** card, once a handful of positions have closed. The
number to watch is whether high-conviction positions actually outperform the
small ones — if they don't, the bot's confidence carries no information, and it
says so itself in that card.

---

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `last_error` mentions `401` | Wrong or rotated API key | Fix `.env`, then restart the service/task |
| `cycles: 0` all day, `market_open: false` during US hours | Clock unreachable — network | Check the laptop's connection |
| `uptime_s` keeps resetting | Crash loop | `journalctl -u stocktrader -n 100` (Linux) or Task Scheduler → Last Run Result (Windows) |
| Dashboard fine, nothing in Alpaca | Orders failed risk checks or were vetoed | Read the orders table and auditor card — it says which |
| Everything stopped overnight | Machine slept | Confirm it's on mains; re-run the installer |
| Can't reach it from your phone | Firewall | Allow port 8080 on the private network |

**Timezone note:** US markets are open 09:30–16:00 New York, which is
**15:30–22:00 in Switzerland** (14:30–21:00 during the few weeks the two
countries' clocks are out of step). Nothing will happen outside those hours —
that's correct, not a fault.

---

## Cost

Only the Anthropic API — Alpaca paper trading is free.

The triage gate keeps quiet cycles to roughly **$0.002**; a full research cycle
is around **$0.15**. At a 30-minute cadence with most cycles triaged out,
budget roughly **$1–3/month**, plus a few watts of electricity.

`CYCLE_MINUTES` in `.env` trades cost against responsiveness. Below ~15 minutes
you're mostly paying for noise: the bot holds positions for days, so checking
every 5 minutes buys nothing.

---

## Keeping it updated

```bash
cd stock-trader
git pull
sudo systemctl restart stocktrader     # Linux
# Windows: Stop-ScheduledTask / Start-ScheduledTask -TaskName "AI Stockbroker"
```

Your `.env` is git-ignored and your trading history in `docs/data/` is local to
that machine, so neither is touched by a pull.
