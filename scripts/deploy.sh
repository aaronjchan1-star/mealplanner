#!/usr/bin/env bash
# Deploy / update mealplanner on the Pi from the GitHub repo.
# Run on the Pi:
#     bash scripts/deploy.sh
# Or remotely from Windows:
#     ssh admin@<pi-ip> "cd mealplanner && bash scripts/deploy.sh"

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> in $PROJECT_DIR"

# 1. Pull latest from git
echo "==> git pull"
git pull --ff-only

# 2. Update Python deps if requirements.txt changed since last run
PIP="$PROJECT_DIR/.venv/bin/pip"
if [[ requirements.txt -nt .venv/.last_install || ! -f .venv/.last_install ]]; then
  echo "==> requirements changed, reinstalling deps"
  "$PIP" install --upgrade --extra-index-url https://www.piwheels.org/simple -r requirements.txt
  touch .venv/.last_install
else
  echo "==> requirements unchanged, skipping pip install"
fi

# 3. Restart the service
echo "==> restarting mealplanner service"
sudo systemctl restart mealplanner

# 4. Show status briefly so we know it's healthy
sleep 2
sudo systemctl status mealplanner --no-pager --lines=5 || true

echo "==> done. tail logs with: journalctl -u mealplanner -f"
