"""Entry point for one trading cycle.

    python -m bot.run              # normal cycle (paper by default)
    python -m bot.run --force      # send orders even if the market is closed
    python -m bot.run --dry-run    # research + plan, but never place orders

Environment variables (see .env.example) control keys, mode and risk limits.
"""
from __future__ import annotations

import argparse
import sys

from .config import Config
from .engine import Engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one AI stockbroker cycle.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Place orders even when the market is closed (they queue for next open).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do the research and build the plan, but do not place any orders.",
    )
    args = parser.parse_args()

    cfg = Config()
    if args.dry_run:
        cfg.dry_run = True

    banner = "LIVE - REAL MONEY" if cfg.is_live else "PAPER - sandbox (fake money)"
    print(f"=== AI Stockbroker | {banner} | model={cfg.model} ===")

    engine = Engine(cfg)
    try:
        engine.run(force=args.force)
    except SystemExit:
        raise
    except Exception as e:  # keep CI logs readable but surface the failure
        print(f"[error] run failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
