#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
BEGIN_MARKER="# BEGIN Hit List Magic Tracker daily update"
END_MARKER="# END Hit List Magic Tracker daily update"
CRON_ENTRY="0 7 * * * $SCRIPT_DIR/run_daily_update.sh >> $PROJECT_DIR/data/daily_update.log 2>&1"
CURRENT_FILE=$(mktemp)
UPDATED_FILE=$(mktemp)
trap 'rm -f "$CURRENT_FILE" "$UPDATED_FILE"' EXIT HUP INT TERM

crontab -l > "$CURRENT_FILE" 2>/dev/null || :
awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == begin { managed = 1; next }
  $0 == end { managed = 0; next }
  !managed { print }
' "$CURRENT_FILE" > "$UPDATED_FILE"

if [ -s "$UPDATED_FILE" ]; then
  printf '\n' >> "$UPDATED_FILE"
fi
printf '%s\n%s\n%s\n' "$BEGIN_MARKER" "$CRON_ENTRY" "$END_MARKER" >> "$UPDATED_FILE"

crontab "$UPDATED_FILE"
echo "Installed daily update for 7:00 AM local time."
echo "Log: $PROJECT_DIR/data/daily_update.log"