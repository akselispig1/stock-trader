#!/usr/bin/env bash
# Turn this machine into an always-on AI stockbroker server (Linux).
#
#   sudo ./deploy/install-linux.sh
#
# Installs a systemd service that starts at boot and restarts on crash, and
# (for a laptop) stops the machine suspending when you close the lid.
# Re-running it is safe - every step is idempotent.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=stocktrader
UNIT=/etc/systemd/system/${SERVICE}.service

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Run with sudo:  sudo $0"; exit 1
fi

# The service must run as a real user, not root: it needs the repo's .env and
# should not have more privilege than the bot requires.
RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
  echo "❌ Run this with sudo from your normal user account, not as root directly."
  echo "   (The service needs to run as you, so it can read $REPO/.env)"
  exit 1
fi

echo "▶ Installing AI Stockbroker as a service"
echo "  repo: $REPO"
echo "  user: $RUN_USER"
echo

# --- 1. Prerequisites ------------------------------------------------------
if [ ! -f "$REPO/.env" ]; then
  echo "❌ No .env found at $REPO/.env"
  echo "   Run ./run-local.sh first to create it, fill in your three API keys,"
  echo "   confirm the bot works, then re-run this installer."
  exit 1
fi
if grep -qE '^(ANTHROPIC_API_KEY=(sk-ant-\.\.\.)?$|ALPACA_API_KEY=$|ALPACA_SECRET_KEY=$)' "$REPO/.env"; then
  echo "❌ .env still has blank keys. Fill all three in, then re-run."; exit 1
fi

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "▶ Creating the Python environment..."
  sudo -u "$RUN_USER" python3 -m venv "$REPO/.venv"
fi
echo "▶ Installing dependencies..."
sudo -u "$RUN_USER" "$REPO/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

# --- 2. The service --------------------------------------------------------
echo "▶ Writing $UNIT"
sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$REPO|g" \
    "$REPO/deploy/stocktrader.service" > "$UNIT"
chmod 644 "$UNIT"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

# --- 3. Laptop: don't sleep ------------------------------------------------
# A laptop suspends on lid-close and often on idle. Either one silently stops
# the bot mid-session, which is the single most likely way this setup fails.
echo "▶ Preventing sleep (this machine is a server now)"
LOGIND=/etc/systemd/logind.conf
cp "$LOGIND" "${LOGIND}.stocktrader.bak" 2>/dev/null || true
for k in HandleLidSwitch HandleLidSwitchExternalPower HandleLidSwitchDocked; do
  if grep -qE "^#?${k}=" "$LOGIND"; then
    sed -i "s|^#\?${k}=.*|${k}=ignore|" "$LOGIND"
  else
    echo "${k}=ignore" >> "$LOGIND"
  fi
done
systemctl restart systemd-logind 2>/dev/null || true
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true

# --- 4. Report -------------------------------------------------------------
sleep 3
echo
if systemctl is-active --quiet "$SERVICE"; then
  echo "✅ Running. It will start again automatically on every boot."
else
  echo "⚠️  Service is not active. Check:  journalctl -u $SERVICE -n 50 --no-pager"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "   Dashboard:  http://localhost:8080"
[ -n "$IP" ] && echo "   From another device on your wifi:  http://$IP:8080"
echo "   Health:     http://localhost:8080/healthz"
echo
echo "   Follow the logs:   journalctl -u $SERVICE -f"
echo "   Stop / start:      sudo systemctl stop|start $SERVICE"
echo "   Undo everything:   sudo ./deploy/uninstall-linux.sh"
