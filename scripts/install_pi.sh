#!/usr/bin/env bash
# First-time install script for the Pi.
# Run from the project root: bash scripts/install_pi.sh
#
# Auto-detects the current user — works whether you're 'pi', 'admin', or anything else.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USERNAME="$(whoami)"
USER_HOME="$HOME"

echo "==> project at $PROJECT_DIR"
echo "==> user is $USERNAME, home is $USER_HOME"

# 1. System deps
echo "==> installing system packages"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip sqlite3 git

# 2. Virtualenv
if [[ ! -d "$PROJECT_DIR/.venv" ]]; then
  echo "==> creating venv"
  python3 -m venv "$PROJECT_DIR/.venv"
fi

# 3. Python deps. Pi Zero W is ARMv6 — wheels are scarce. Pull from piwheels.
PIP="$PROJECT_DIR/.venv/bin/pip"
"$PIP" install --upgrade pip wheel
"$PIP" install --extra-index-url https://www.piwheels.org/simple -r "$PROJECT_DIR/requirements.txt"
touch "$PROJECT_DIR/.venv/.last_install"

# 4. Config
if [[ ! -f "$PROJECT_DIR/config.py" ]]; then
  echo "==> creating config.py from example"
  cp "$PROJECT_DIR/config.example.py" "$PROJECT_DIR/config.py"
  # Patch the DB_PATH so it points at this project's directory, not /home/pi/...
  sed -i "s|/home/pi/mealplanner|$PROJECT_DIR|g" "$PROJECT_DIR/config.py"
  echo "    edit $PROJECT_DIR/config.py to set ANTHROPIC_API_KEY"
  echo "    OR put it in $PROJECT_DIR/.env  (recommended — gitignored)"
fi

# 5. systemd unit — generate it from the template with the right user/paths
SERVICE_TMPL="$PROJECT_DIR/systemd/mealplanner.service"
SERVICE_DEPLOYED="/etc/systemd/system/mealplanner.service"

echo "==> generating systemd unit for user '$USERNAME' at '$PROJECT_DIR'"
sudo bash -c "sed -e 's|User=pi|User=$USERNAME|' \
                  -e 's|/home/pi/mealplanner|$PROJECT_DIR|g' \
                  '$SERVICE_TMPL' > '$SERVICE_DEPLOYED'"
sudo systemctl daemon-reload
sudo systemctl enable mealplanner.service

cat <<MSG

==> install done.

Next:
  1. Set your Anthropic API key.  Easiest:
       echo 'ANTHROPIC_API_KEY=sk-ant-...' > $PROJECT_DIR/.env
       chmod 600 $PROJECT_DIR/.env

  2. Start the service:
       sudo systemctl start mealplanner
       sudo systemctl status mealplanner

  3. Open http://<pi-ip>:8080 from a browser on the same network.

To follow logs:
  journalctl -u mealplanner -f

To deploy future updates after pushing to GitHub:
  bash $PROJECT_DIR/scripts/deploy.sh
MSG
