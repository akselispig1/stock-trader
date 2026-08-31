#!/usr/bin/env bash
# Undo deploy/install-linux.sh: stop the service, remove it, and let the
# machine sleep again. Leaves the repo, .env and all trading data untouched.
set -euo pipefail

SERVICE=stocktrader
UNIT=/etc/systemd/system/${SERVICE}.service
LOGIND=/etc/systemd/logind.conf

if [ "$(id -u)" -ne 0 ]; then echo "❌ Run with sudo:  sudo $0"; exit 1; fi

echo "▶ Stopping and removing the service"
systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true
rm -f "$UNIT"
systemctl daemon-reload

echo "▶ Allowing sleep again"
systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
if [ -f "${LOGIND}.stocktrader.bak" ]; then
  mv "${LOGIND}.stocktrader.bak" "$LOGIND"
  systemctl restart systemd-logind 2>/dev/null || true
  echo "  restored $LOGIND from backup"
else
  echo "  ⚠ no logind backup found - if the lid still does nothing, set"
  echo "    HandleLidSwitch=suspend in $LOGIND by hand"
fi

echo
echo "✅ Removed. Your repo, .env and trading history are untouched."
echo "   Run the bot by hand any time with:  ./run-local.sh"
