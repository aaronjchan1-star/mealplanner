#!/usr/bin/env bash
# Backup the mealplanner database from the Pi to the Synology NAS.
#
# RUNS ON THE NAS (not the Pi). The NAS connects out to the Pi via rsync over SSH,
# pulls a copy of data.db, and rotates daily/weekly backups.
#
# Setup steps before scheduling this:
#   1. Enable SSH on the NAS (Control Panel → Terminal & SNMP → Enable SSH service)
#   2. SSH from NAS to Pi once and accept the host key:
#        ssh admin@<pi-ip> echo ok
#   3. Set up passwordless SSH from NAS to Pi (skip if you'll always be there to type a password):
#        On the NAS:
#          ssh-keygen -t ed25519 -f ~/.ssh/id_pi_backup -N ""
#          ssh-copy-id -i ~/.ssh/id_pi_backup.pub admin@<pi-ip>
#   4. Edit the variables below.
#   5. Schedule it in DSM:
#        Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script
#        Run as: your DSM admin user
#        Schedule: daily at 3am
#        Run command: bash /volume1/backups/mealplanner/backup.sh

set -euo pipefail

# --- EDIT THESE ---
PI_HOST="admin@192.168.1.42"               # change to your Pi's user@ip
PI_DB_PATH="/home/admin/mealplanner/data.db"
BACKUP_ROOT="/volume1/backups/mealplanner" # where to keep backups on the NAS
SSH_KEY="$HOME/.ssh/id_pi_backup"          # or "" to use default key
KEEP_DAILY=14                              # number of daily backups to retain
# ------------------

mkdir -p "$BACKUP_ROOT/daily"

DATE_STAMP="$(date +%Y-%m-%d)"
DEST="$BACKUP_ROOT/daily/data-$DATE_STAMP.db"

SSH_OPTS=""
if [[ -n "$SSH_KEY" && -f "$SSH_KEY" ]]; then
  SSH_OPTS="-i $SSH_KEY"
fi

echo "==> $(date) — pulling $PI_DB_PATH from $PI_HOST"

# rsync uses sqlite3 .backup over SSH-piped command for a consistent copy,
# avoiding the WAL issue where rsync of a live DB might miss recent writes.
ssh $SSH_OPTS "$PI_HOST" "sqlite3 '$PI_DB_PATH' \".backup '/tmp/mealplanner-backup.db'\""
rsync -av $([[ -n "$SSH_OPTS" ]] && echo "-e \"ssh $SSH_OPTS\"") \
  "$PI_HOST:/tmp/mealplanner-backup.db" "$DEST"
ssh $SSH_OPTS "$PI_HOST" "rm /tmp/mealplanner-backup.db"

echo "==> wrote $DEST"

# Rotate: keep only the last $KEEP_DAILY daily backups
cd "$BACKUP_ROOT/daily"
ls -1t data-*.db 2>/dev/null | tail -n +$((KEEP_DAILY + 1)) | xargs -r rm --
REMAINING="$(ls -1 data-*.db 2>/dev/null | wc -l)"

echo "==> kept $REMAINING daily backups"
echo "==> done"
