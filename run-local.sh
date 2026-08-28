#!/usr/bin/env bash
# One-command local launcher (macOS / Linux).
#   ./run-local.sh          -> trading loop + dashboard at http://localhost:8080
#   ./run-local.sh once     -> run a single cycle and exit
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "❌ Python 3 not found. Install it from python.org, then re-run."; exit 1; }

if [ ! -d .venv ]; then
  echo "📦 Creating a private Python environment (one time)..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "🔑 Created a .env file for your API keys."
  echo "   Open it and fill in ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY,"
  echo "   then run this script again."
  echo "   (.env is git-ignored - your keys never leave this computer.)"
  exit 1
fi
if grep -qE '^(ANTHROPIC_API_KEY=(sk-ant-\.\.\.)?$|ALPACA_API_KEY=$|ALPACA_SECRET_KEY=$)' .env; then
  echo "🔑 .env still has blank keys - fill all three in, then re-run."; exit 1
fi

if [ "${1:-}" = "once" ]; then
  echo "▶️  Running a single cycle..."
  exec python -m bot.run
fi

echo ""
echo "✅ Starting. Dashboard: http://localhost:8080   (Ctrl+C to stop)"
echo ""
exec python -m bot.serve
