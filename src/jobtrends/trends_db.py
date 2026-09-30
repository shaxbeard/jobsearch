#!/usr/bin/env python3
"""
SQLite persistence for the per-city programming-language demand dataset.

Both language_trends.py (direct ATS crawl) and google_language_trends.py
(Google-discovery + full-JD fetch) write here. Rather than keeping a growing
history of separate "runs", this stores ONE combined, always-current dataset:
each time a city is processed, that city's rows are REPLACED in place, so the
database always reflects the latest data for every city that's been captured.
Re-running a subset of cities updates just those cities and leaves the rest
untouched -- so you can refresh cities independently and still query one
unified dataset.

Data is scoped by (city, source) so the two methodologies don't clobber each
other: re-running Google for "seattle" replaces only the source='google' rows
for Seattle; any source='ats' rows for Seattle are left alone.

Stored in a local SQLite file (stdlib `sqlite3`, no extra dependency, no
server to run).

Schema:
  cities                -- one row per (city, source): total matched, since-date, last-updated
  city_language_counts  -- one row per (city, source, language): rank/count/percent
  postings              -- one row per (city, source, job posting), full description text
                           included, so a UI can let someone click a city (or a
                           language within a city) and drill into the underlying
                           job ads.

Usage (as a library):
    from trends_db import get_connection, ensure_schema, update_city

    conn = get_connection(db_path)
    ensure_schema(conn)
    update_city(
        conn,
        city="dallas",
        source="google",
        since_date="2026-01-01",
        total_matched=93,
        ranked=[...],           # list of {language, rank, count, percent}
        postings=[...],         # list of full posting dicts (see update_city docstring)
    )
    conn.close()

Querying language demand for a city (latest, combined dataset):
    sqlite3 job_trends.db "
      SELECT language, count, percent FROM city_language_counts
      WHERE city = 'dallas' AND source = 'google'
      ORDER BY rank;"

Drilling into the actual job ads behind a language:
    sqlite3 job_trends.db "
      SELECT company, title, url FROM postings
      WHERE city = 'dallas' AND source = 'google'
        AND matched_languages LIKE '%Java%';"
"""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

# Where the SQLite file lives. Defaults to the repository root for local use,
# but can be pointed at a mounted persistent disk in production (e.g. Render)
# by setting the JOB_TRENDS_DB environment variable. Resolution (including that
# override) lives in jobtrends.paths so every module shares one definition.
from jobtrends.paths import DEFAULT_DB_PATH
STATS_WINDOW_MONTHS = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS cities (
    city TEXT NOT NULL,
    source TEXT NOT NULL,
    total_matched INTEGER NOT NULL,
    since_date TEXT,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (city, source)
);

CREATE TABLE IF NOT EXISTS city_language_counts (
    city TEXT NOT NULL,
    source TEXT NOT NULL,
    language TEXT NOT NULL,
    rank INTEGER NOT NULL,
    count INTEGER NOT NULL,
    percent REAL NOT NULL,
    PRIMARY KEY (city, source, language)
);

CREATE INDEX IF NOT EXISTS idx_clc_city_language ON city_language_counts(city, language);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    posted_at TEXT,
    description TEXT NOT NULL,
    matched_languages TEXT NOT NULL DEFAULT '[]',
    matched_tools TEXT NOT NULL DEFAULT '[]',
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_postings_city ON postings(city, source);
CREATE INDEX IF NOT EXISTS idx_postings_url ON postings(url);
-- Matches the date(posted_at) BETWEEN date(?) AND date(?) filter used by the
-- web app and trends_stats.py, so those lookups don't scan every row.
CREATE INDEX IF NOT EXISTS idx_postings_posted_at_date ON postings(date(posted_at));
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_matched_tools(conn)


def _migrate_matched_tools(conn: sqlite3.Connection) -> None:
    """One-time backfill for DBs created before the matched_tools column.

    CREATE TABLE IF NOT EXISTS (in SCHEMA) doesn't add columns to an existing
    table, so a plain ALTER TABLE is needed for databases that predate this
    column. This only runs its (potentially slow, full-table) backfill once --
    once the column exists, the cheap PRAGMA check below short-circuits it on
    every subsequent call (ensure_schema runs on every new connection).
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(postings)").fetchall()}
    if "matched_tools" in columns:
        return
    from jobtrends.language_detect import tools_in_posting

    conn.execute("ALTER TABLE postings ADD COLUMN matched_tools TEXT NOT NULL DEFAULT '[]'")
    rows = conn.execute("SELECT id, title, description FROM postings").fetchall()
    conn.executemany(
        "UPDATE postings SET matched_tools = ? WHERE id = ?",
        [
            (json.dumps(tools_in_posting({"title": title or "", "description": description or ""})), post_id)
            for post_id, title, description in rows
        ],
    )
    conn.commit()


def update_city(
    conn: sqlite3.Connection,
    *,
    city: str,
    source: str,
    since_date: str,
    total_matched: int,
    ranked: list[dict],
    postings: list[dict] | None = None,
    store_postings: bool = True,
) -> None:
    """Replace this (city, source)'s data with the latest results, in place.

    This deletes the city's existing language counts (and postings, when
    store_postings is True) for the given source and re-inserts the new ones,
    so the combined dataset stays current without accumulating duplicate
    history. Other cities -- and the same city under a different source -- are
    untouched.

    `source` is 'ats' (language_trends.py) or 'google' (google_language_trends.py).
    `ranked` is a list of {language, rank, count, percent} dicts.
    Each posting dict must have: company, platform, title, location, url,
    posted_at, description, matched_languages (list[str]).

    When store_postings is False (e.g. --no-postings), the language counts are
    still refreshed but the city's existing postings are left as-is.
    """
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO cities (city, source, total_matched, since_date, updated_at_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(city, source) DO UPDATE SET
            total_matched = excluded.total_matched,
            since_date = excluded.since_date,
            updated_at_utc = excluded.updated_at_utc
        """,
        (city, source, total_matched, since_date, now),
    )

    conn.execute("DELETE FROM city_language_counts WHERE city = ? AND source = ?", (city, source))
    conn.executemany(
        """
        INSERT INTO city_language_counts (city, source, language, rank, count, percent)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (city, source, row["language"], row["rank"], row["count"], row["percent"])
            for row in ranked
        ],
    )

    if store_postings:
        conn.execute("DELETE FROM postings WHERE city = ? AND source = ?", (city, source))
        if postings:
            conn.executemany(
                """
                INSERT INTO postings
                    (city, source, company, platform, title, location, url, posted_at, description, matched_languages, matched_tools, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        city,
                        source,
                        posting["company"],
                        posting["platform"],
                        posting["title"],
                        posting["location"],
                        posting["url"],
                        posting["posted_at"],
                        posting["description"],
                        json.dumps(posting["matched_languages"]),
                        json.dumps(posting.get("matched_tools", [])),
                        now,
                    )
                    for posting in postings
                ],
            )

    conn.commit()


def get_last_fetched(conn: sqlite3.Connection, *, city: str, source: str) -> str | None:
    """Return the ISO timestamp this (city, source) was last updated, or None
    if it has never been fetched. Used to fetch only postings newer than the
    last run (incremental updates)."""
    row = conn.execute(
        "SELECT updated_at_utc FROM cities WHERE city = ? AND source = ?",
        (city, source),
    ).fetchone()
    return row[0] if row else None


def get_postings(conn: sqlite3.Connection, *, city: str, source: str) -> list[dict]:
    """Return all stored postings for a (city, source), with matched_languages
    decoded back into a list. Used to dedupe against already-stored postings
    and to re-rank a city's accumulated data."""
    columns = [
        "company",
        "platform",
        "title",
        "location",
        "url",
        "posted_at",
        "description",
        "matched_languages",
        "matched_tools",
    ]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM postings WHERE city = ? AND source = ?",
        (city, source),
    ).fetchall()
    postings = []
    for row in rows:
        posting = dict(zip(columns, row))
        posting["matched_languages"] = json.loads(posting["matched_languages"])
        posting["matched_tools"] = json.loads(posting["matched_tools"])
        postings.append(posting)
    return postings


def get_posting_urls(conn: sqlite3.Connection, *, city: str, source: str) -> set[str]:
    """Return just the stored URLs for a (city, source).

    Cheap dedup-check alternative to get_postings() -- avoids loading full rows
    (including the large description column) for cities with a lot of history.
    """
    rows = conn.execute(
        "SELECT url FROM postings WHERE city = ? AND source = ?",
        (city, source),
    ).fetchall()
    return {row[0] for row in rows}


def count_postings(conn: sqlite3.Connection, *, city: str, source: str) -> int:
    """Return how many postings are stored for a (city, source)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM postings WHERE city = ? AND source = ?",
        (city, source),
    ).fetchone()
    return row[0] if row else 0


def get_recent_postings_summary(
    conn: sqlite3.Connection,
    *,
    city: str,
    source: str,
    since_date: str,
) -> list[dict]:
    """Return postings advertised on/after since_date, WITHOUT the large
    description column.

    Used for re-ranking/re-summarizing a city's rolling stats window, which
    never needs description text -- unlike get_postings(), this filters at the
    SQL level (using the date(posted_at) index) instead of loading every
    historical posting (full description included) for the city into memory.
    """
    columns = [
        "company",
        "platform",
        "title",
        "location",
        "url",
        "posted_at",
        "matched_languages",
        "matched_tools",
    ]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM postings "
        "WHERE city = ? AND source = ? AND date(posted_at) >= date(?)",
        (city, source, since_date),
    ).fetchall()
    postings = []
    for row in rows:
        posting = dict(zip(columns, row))
        posting["matched_languages"] = json.loads(posting["matched_languages"])
        posting["matched_tools"] = json.loads(posting["matched_tools"])
        postings.append(posting)
    return postings


def stats_cutoff_date(now: datetime | None = None) -> str:
    """Return the inclusive YYYY-MM-DD cutoff for the rolling stats window."""
    current = (now or datetime.now(timezone.utc)).date()
    month_index = current.year * 12 + current.month - 1 - STATS_WINDOW_MONTHS
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(current.day, monthrange(year, month)[1])
    return current.replace(year=year, month=month, day=day).isoformat()


def posting_is_in_stats_window(posting: dict, cutoff: str | None = None) -> bool:
    """Whether a posting's advertised date is inside the rolling stats window."""
    posted_at = str(posting.get("posted_at") or "")
    return len(posted_at) >= 10 and posted_at[:10] >= (cutoff or stats_cutoff_date())


def get_postings_in_stats_window(
    conn: sqlite3.Connection,
    *,
    city: str,
    source: str,
) -> list[dict]:
    """Return recent postings without deleting older retained history."""
    cutoff = stats_cutoff_date()
    return [
        posting
        for posting in get_postings(conn, city=city, source=source)
        if posting_is_in_stats_window(posting, cutoff)
    ]


def get_postings_in_date_range(
    conn: sqlite3.Connection,
    *,
    city: str,
    source: str,
    start_date: str | None,
    end_date: str,
) -> list[dict]:
    """Return retained postings inside an inclusive advertised-date range."""
    return [
        posting
        for posting in get_postings(conn, city=city, source=source)
        if len(str(posting.get("posted_at") or "")) >= 10
        and (start_date is None or str(posting["posted_at"])[:10] >= start_date)
        and str(posting["posted_at"])[:10] <= end_date
    ]


def count_postings_by_city_in_date_range(
    conn: sqlite3.Connection,
    *,
    source: str,
    start_date: str,
    end_date: str,
) -> dict[str, int]:
    """Per-city posting counts inside an inclusive advertised-date range.

    Same date filter as get_postings_in_date_range, but computed in one SQL
    query (index-assisted, no per-city round trip and no description/full-row
    transfer) -- use this where only a count per city is needed, such as the
    map overview, instead of calling get_postings_in_date_range per city.
    """
    rows = conn.execute(
        "SELECT city, COUNT(*) FROM postings "
        "WHERE source = ? AND date(posted_at) BETWEEN date(?) AND date(?) "
        "GROUP BY city",
        (source, start_date, end_date),
    ).fetchall()
    return dict(rows)


def add_postings(
    conn: sqlite3.Connection,
    *,
    city: str,
    source: str,
    postings: list[dict],
) -> None:
    """Append new postings for a (city, source) WITHOUT deleting existing ones.

    This is the incremental counterpart to update_city: callers fetch only
    postings newer than the last run and add them here, so data already stored
    is never re-fetched or overwritten. The caller is responsible for deduping
    (not passing postings that are already stored). Each posting dict must have:
    company, platform, title, location, url, posted_at, description,
    matched_languages (list[str]).
    """
    if not postings:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO postings
            (city, source, company, platform, title, location, url, posted_at, description, matched_languages, matched_tools, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                city,
                source,
                posting["company"],
                posting["platform"],
                posting["title"],
                posting["location"],
                posting["url"],
                posting["posted_at"],
                posting["description"],
                json.dumps(posting["matched_languages"]),
                json.dumps(posting.get("matched_tools", [])),
                now,
            )
            for posting in postings
        ],
    )
    conn.commit()


def set_city_counts(
    conn: sqlite3.Connection,
    *,
    city: str,
    source: str,
    since_date: str,
    total_matched: int,
    ranked: list[dict],
) -> None:
    """Replace a (city, source)'s aggregate language counts and refresh its
    metadata (total_matched, since_date, and the last-fetched timestamp).

    Language counts are always fully replaced because they're aggregates over
    the city's full accumulated posting set -- recompute them from get_postings
    after add_postings, then store them here.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO cities (city, source, total_matched, since_date, updated_at_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(city, source) DO UPDATE SET
            total_matched = excluded.total_matched,
            since_date = excluded.since_date,
            updated_at_utc = excluded.updated_at_utc
        """,
        (city, source, total_matched, since_date, now),
    )
    conn.execute("DELETE FROM city_language_counts WHERE city = ? AND source = ?", (city, source))
    conn.executemany(
        """
        INSERT INTO city_language_counts (city, source, language, rank, count, percent)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (city, source, row["language"], row["rank"], row["count"], row["percent"])
            for row in ranked
        ],
    )
    conn.commit()

