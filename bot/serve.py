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
import threading
import time
from datetime import datetime, timezone
from functools import partial

from .alpaca import Alpaca, AlpacaError
from .config import Config
from .engine import Engine

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs")

# Shared status, surfaced at /healthz so you can see the loop is alive.
STATUS: dict = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "cycles": 0,
    "last_run": None,
    "last_result": None,
    "last_error": None,
    "next_check_in_s": None,
}


def _sleep_seconds(cfg: Config, market_open: bool) -> int:
    """Wait a normal interval while trading; back off when the market is shut."""
    if market_open:
        return max(60, int(cfg.cycle_minutes * 60))
    return max(300, int(cfg.closed_poll_minutes * 60))


def trading_loop(cfg: Config) -> None:
    """Run cycles forever. Never let one bad cycle kill the process."""
    engine = Engine(cfg)
    alpaca = Alpaca(cfg)
    print(f"[serve] loop started · every {cfg.cycle_minutes}min while open, "
          f"polling every {cfg.closed_poll_minutes}min while closed")
    while True:
        market_open = False
        try:
            market_open = bool(alpaca.clock().get("is_open"))
        except AlpacaError as e:
            STATUS["last_error"] = f"clock: {e}"[:200]

        if market_open:
            try:
                state = engine.run()
                STATUS["cycles"] += 1
                STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
                STATUS["last_result"] = (state.get("market_summary") or "")[:200]
                STATUS["last_error"] = None
            except Exception as e:  # a failed cycle must not stop the loop
                STATUS["last_error"] = str(e)[:300]
                print(f"[serve] cycle failed (continuing): {e}")
        else:
            print("[serve] market closed - skipping cycle")

        wait = _sleep_seconds(cfg, market_open)
        STATUS["next_check_in_s"] = wait
        time.sleep(wait)


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the dashboard, plus a /healthz status endpoint."""

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.split("?")[0] in ("/healthz", "/status"):
            body = json.dumps(STATUS, indent=2).encode()
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


def serve_dashboard(port: int) -> None:
    handler = partial(Handler, directory=DOCS_DIR)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"[serve] dashboard on :{port} (health at /healthz)")
    httpd.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description="Always-on trading loop + dashboard.")
    ap.add_argument("--no-web", action="store_true", help="Run the loop without the HTTP server.")
    args = ap.parse_args()

    cfg = Config()
    cfg.validate()
    banner = "LIVE - REAL MONEY" if cfg.is_live else "PAPER - sandbox (fake money)"
    print(f"=== AI Stockbroker (always-on) | {banner} | model={cfg.model} ===")

    if args.no_web:
        trading_loop(cfg)
        return 0

    # Loop in the background; the HTTP server owns the main thread so the
    # platform's port check succeeds immediately.
    threading.Thread(target=trading_loop, args=(cfg,), daemon=True).start()
    serve_dashboard(int(os.getenv("PORT", "8080")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
