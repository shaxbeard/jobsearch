"""In-process daily-update scheduler for single-instance deployments (e.g. Render).

A hosted web service's persistent disk is attached to just that one service, so
a separate scheduled-job resource wouldn't see the same `job_trends.db` file.
Instead, the daily Google-trends update runs as a background thread inside the
same process that serves the Flask app. Only enable this in that deployment
(set JOBTRENDS_ENABLE_SCHEDULER=1) -- local `jobtrends-web` runs keep relying on
the existing macOS cron (scripts/run_daily_update.sh) against the same file.

Run the web service with a single worker (e.g. `gunicorn --workers 1 ...`) so
only one scheduler thread ever exists; the lock directory below is a cheap
extra guard against overlapping runs, not a substitute for that.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from jobtrends.paths import PROJECT_ROOT

_LOCK_DIR = os.path.join(tempfile.gettempdir(), "jobtrends-scheduler-update.lock")

_started = False
_start_lock = threading.Lock()


def _seconds_until_next_run(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _run_update() -> None:
    try:
        os.mkdir(_LOCK_DIR)
    except FileExistsError:
        print("scheduler: skipped, another update is already running", flush=True)
        return
    try:
        print(f"{datetime.now(timezone.utc).isoformat()} scheduler: daily update started", flush=True)
        args = [sys.executable, "-m", "jobtrends.analysis.google_language_trends", "--insecure"]
        if os.environ.get("SERPER_PAID") == "1":
            args.append("--paid")
        # Caps concurrent fetches on memory-constrained hosts (e.g. Render's 512MB plan).
        max_workers = os.environ.get("JOBTRENDS_FETCH_MAX_WORKERS")
        if max_workers:
            args += ["--max-workers", max_workers]
        process = subprocess.Popen(args, cwd=PROJECT_ROOT)
        # Linux-only: if memory runs out, have the kernel kill this job rather than the web server.
        try:
            with open(f"/proc/{process.pid}/oom_score_adj", "w") as oom_file:
                oom_file.write("1000")
        except OSError:
            pass
        returncode = process.wait()
        print(
            f"{datetime.now(timezone.utc).isoformat()} scheduler: daily update finished "
            f"with status {returncode}"
            + (" (killed by signal, likely out of memory)" if returncode < 0 else ""),
            flush=True,
        )
    finally:
        os.rmdir(_LOCK_DIR)


def _loop(hour_utc: int) -> None:
    while True:
        time.sleep(_seconds_until_next_run(hour_utc))
        try:
            _run_update()
        except Exception as exc:  # keep the loop alive even if one run raises
            print(f"scheduler: daily update raised {exc!r}", flush=True)


def start(hour_utc: int = 11) -> None:
    """Start the background daily-update loop once per process."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_loop, args=(hour_utc,), daemon=True, name="jobtrends-scheduler")
    thread.start()
    print(f"scheduler: enabled, daily update scheduled for {hour_utc:02d}:00 UTC", flush=True)
