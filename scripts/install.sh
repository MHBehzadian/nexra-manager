#!/usr/bin/env bash
#
# Telegram Automation — installer.
# Run it from inside the cloned repo:  bash scripts/install.sh
#
set -euo pipefail

# Move to the repo root (this script lives in scripts/).
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Telegram Automation installer"
echo "    repo: $(pwd)"

if command -v apt-get >/dev/null 2>&1; then
  echo "==> Installing system packages (python3, venv, pip, git)…"
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip git
fi

echo "==> Creating virtualenv (.venv)…"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

[ -f .env ] || cp .env.example .env

cat <<'EOF'

✅ Installed.

Next steps
----------
1) Configure once (writes .env), then Ctrl-C after "Bot online":
     . .venv/bin/activate && python main.py
   (or edit .env directly and run again)

2) Install the systemd service (edit paths in the unit file if you cloned
   somewhere other than /opt/telegram-automation):
     sudo cp deploy/nexra-manager.service /etc/systemd/system/
     sudo systemctl daemon-reload
     sudo systemctl enable --now nexra-manager

3) Follow logs:
     journalctl -u nexra-manager -f

Then open your bot in Telegram and send /start.
EOF
