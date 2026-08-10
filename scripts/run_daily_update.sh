#!/bin/sh

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-/opt/miniconda3/bin/python}
LOCK_DIR="${TMPDIR:-/tmp}/hit-list-magic-update.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') update skipped: another run is active"
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT HUP INT TERM

cd "$PROJECT_DIR" || exit 1
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
echo "$(date '+%Y-%m-%d %H:%M:%S') daily update started"
"$PYTHON_BIN" -m jobtrends.analysis.google_language_trends --insecure
exit_code=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') daily update finished with status $exit_code"
exit "$exit_code"