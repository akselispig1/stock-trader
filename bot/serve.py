"""Always-on runner: the trading loop plus a live dashboard, in one process.

Why this exists: GitHub's scheduled Actions are best-effort and drop runs (we
have seen a whole trading day with zero). This module is the reliable
alternative - a normal long-lived process that decides for itself when to
trade, using a real clock.

It also SERVES the dashboard from ./docs. That matters because a hosted
process cannot commit data back to GitHub, so instead of pushing state.json to
Pages, the service just serves its own always-current copy. One URL, always up
to date, no git involved.

    python -m bot.serve            # loop + dashboard on $PORT (default 8080)
    python -m bot.serve --no-web   # loop only, no HTTP server

Runs anywhere a Python process can: Render/Railway/Fly, a VPS, or your own
machine. Configuration is the same environment variables as the cron.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial

from .alpaca import Alpaca, AlpacaError
from .config import Config
from .engine import Engine

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs")

# Set by SIGTERM/SIGINT. The loop waits on this instead of sleeping, so a
# service stop takes effect immediately rather than after a 30-minute nap, and
# never interrupts a cycle midway through placing orders.
STOP = threading.Event()

_STARTED = time.time()

# Shared status, surfaced at /healthz so you can see the loop is alive.
STATUS: dict = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "cycles": 0,
    "errors": 0,
    "last_run": None,
    "last_result": None,
    "last_error": None,
    "last_error_at": None,
    "next_check_in_s": None,
    "market_open": None,
}


def log(msg: str) -> None:
    """Timestamped, unbuffered stdout.

    Unbuffered matters on an unattended box: without it the last lines before a
    crash or power cut sit in a buffer and are lost, which is exactly the
    output you need to explain what happened.
    """
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}",
          flush=True)


def _health() -> dict:
    """STATUS plus derived fields worth having when diagnosing a quiet bot."""
    now = time.time()
    out = dict(STATUS)
    out["uptime_s"] = int(now - _STARTED)
    out["stopping"] = STOP.is_set()
    # "It is running" and "it is working" are different questions. A loop can
    # be alive for hours while every cycle fails, so surface staleness plainly.
    last = STATUS.get("last_run")
    if last:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last)).total_seconds()
            out["last_run_age_s"] = int(age)
        except ValueError:
            pass
    out["healthy"] = STATUS["cycles"] > 0 and STATUS["last_error"] is None
    return out


def _sleep_seconds(cfg: Config, market_open: bool) -> int:
    """Wait a normal interval while trading; back off when the market is shut."""
    if market_open:
        return max(60, int(cfg.cycle_minutes * 60))
    return max(300, int(cfg.closed_poll_minutes * 60))


def _handle_signal(signum, _frame) -> None:
    log(f"received signal {signum} - finishing up, will exit after this cycle")
    STOP.set()


def trading_loop(cfg: Config) -> None:
    """Run cycles until asked to stop. Never let one bad cycle kill the process.

    Everything in here is written for an unattended machine: a transient
    network drop, an Alpaca outage or a bad cycle must all be survivable
    without human intervention, because there is nobody watching at 3am.
    """
    engine = Engine(cfg)
    alpaca = Alpaca(cfg)
    log(f"loop started - every {cfg.cycle_minutes}min while open, "
        f"polling every {cfg.closed_poll_minutes}min while closed")

    while not STOP.is_set():
        market_open = False
        try:
            market_open = bool(alpaca.clock().get("is_open"))
            STATUS["market_open"] = market_open
        except AlpacaError as e:
            # Almost always the network, not the account. Log and retry later;
            # a laptop that lost wifi must recover on its own.
            STATUS["last_error"] = f"clock: {e}"[:200]
            STATUS["last_error_at"] = datetime.now(timezone.utc).isoformat()
            STATUS["errors"] += 1
            log(f"could not reach Alpaca ({e}) - will retry")

        if market_open:
            try:
                state = engine.run()
                STATUS["cycles"] += 1
                STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
                STATUS["last_result"] = (state.get("market_summary") or "")[:200]
                STATUS["last_error"] = None
                log(f"cycle {STATUS['cycles']} complete")
            except Exception as e:  # a failed cycle must not stop the loop
                STATUS["last_error"] = str(e)[:300]
                STATUS["last_error_at"] = datetime.now(timezone.utc).isoformat()
                STATUS["errors"] += 1
                log(f"cycle failed (continuing): {e}")
        else:
            log("market closed - skipping cycle")

        wait = _sleep_seconds(cfg, market_open)
        STATUS["next_check_in_s"] = wait
        # Interruptible: a stop signal breaks out immediately instead of
        # leaving systemd to hard-kill us mid-nap.
        STOP.wait(wait)
    log("loop stopped")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the dashboard, plus a /healthz status endpoint."""

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.split("?")[0] in ("/healthz", "/status"):
            body = json.dumps(_health(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # The bot rewrites these files in place; never let a proxy cache them.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):  # keep hosting logs readable
        pass


def serve_dashboard(port: int, host: str = "0.0.0.0") -> None:
    handler = partial(Handler, directory=DOCS_DIR)
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    log(f"dashboard on {host}:{port} (health at /healthz)")

    # Serve in the background so the main thread can wait on the stop signal
    # and shut the socket down cleanly.
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        while not STOP.wait(1.0):
            pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        log("dashboard stopped")


def main() -> int:
    ap = argparse.ArgumentParser(description="Always-on trading loop + dashboard.")
    ap.add_argument("--no-web", action="store_true", help="Run the loop without the HTTP server.")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")),
                    help="Dashboard port (default 8080, or $PORT).")
    ap.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"),
                    help="Bind address. Default 0.0.0.0 so other devices on your "
                         "network can open the dashboard; use 127.0.0.1 to keep it "
                         "to this machine only.")
    args = ap.parse_args()

    # Handle service stop and Ctrl+C the same way: set the flag, let the loop
    # finish what it is doing, exit cleanly. Never abandon a half-placed order.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    cfg = Config()
    cfg.validate()
    banner = "LIVE - REAL MONEY" if cfg.is_live else "PAPER - sandbox (fake money)"
    log(f"=== AI Stockbroker (always-on) | {banner} | model={cfg.model} ===")
    log(f"python {sys.version.split()[0]} | pid {os.getpid()}")

    if args.no_web:
        trading_loop(cfg)
        return 0

    # Loop in the background; the main thread owns the HTTP server so the
    # port is listening immediately (hosting platforms check for that).
    t = threading.Thread(target=trading_loop, args=(cfg,), daemon=True)
    t.start()
    serve_dashboard(args.port, args.host)
    # Give an in-flight cycle a moment to finish writing state.json before the
    # process exits, so the dashboard is never left with a truncated file.
    t.join(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
