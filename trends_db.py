#!/usr/bin/env python3
"""
SQLite persistence for language_trends.py snapshot runs.

Each time language_trends.py runs, it captures a snapshot of "programming
language demand per city" -- a live crawl of Greenhouse/Lever/Ashby's public
job-board APIs at that moment (see language_trends.py's docstring for how
fresh/volatile this data is: postings open and close continuously). This
module stores each snapshot in a local SQLite file (stdlib `sqlite3`, no
extra dependency, no server to run) so trends can be compared over time --
e.g. by putting language_trends.py on a daily/weekly cron job.

Schema:
  runs                  -- one row per script run (when, filters, totals)
  city_language_counts  -- one row per (run, city, language): rank/count/percent
  postings              -- one row per (run, city, job posting), full description text
                           included, so a UI can let someone click a city (or a
                           language within a city) and drill into the underlying
                           job ads. This table grows every run since the same
                           still-open posting gets re-stored each time it's
                           re-crawled -- that's expected, it's what lets you see
                           what a snapshot looked like at any point in time.

Usage (as a library, imported by language_trends.py):
    from trends_db import get_connection, ensure_schema, record_run, record_city_language_counts, record_postings

    conn = get_connection(db_path)
    ensure_schema(conn)
    run_id = record_run(conn, run_at_utc=..., since_date=..., companies_crawled=..., raw_postings_fetched=...)
    record_city_language_counts(conn, run_id=run_id, city="dallas", total_matched=93, ranked=[...])
    record_postings(conn, run_id=run_id, city="dallas", postings=[...])
    conn.close()

Querying the trend history directly, e.g. Java demand in Dallas over time:
    sqlite3 job_trends.db "
      SELECT r.run_at_utc, c.count, c.percent
      FROM city_language_counts c JOIN runs r ON r.id = c.run_id
      WHERE c.city = 'dallas' AND c.language = 'Java'
      ORDER BY r.run_at_utc;"

Drilling into the actual job ads behind a language, from the latest run:
    sqlite3 job_trends.db "
      SELECT company, title, url FROM postings
      WHERE city = 'dallas' AND matched_languages LIKE '%\"Java\"%'
        AND run_id = (SELECT MAX(id) FROM runs)
      ESCAPE '\\';"
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "job_trends.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at_utc TEXT NOT NULL,
    since_date TEXT,
    companies_crawled INTEGER NOT NULL,
    raw_postings_fetched INTEGER NOT NULL,
    failed_companies INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS city_language_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    city TEXT NOT NULL,
    total_matched INTEGER NOT NULL,
    language TEXT NOT NULL,
    rank INTEGER NOT NULL,
    count INTEGER NOT NULL,
    percent REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clc_run ON city_language_counts(run_id);
CREATE INDEX IF NOT EXISTS idx_clc_city_language ON city_language_counts(city, language);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    city TEXT NOT NULL,
    company TEXT NOT NULL,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    posted_at TEXT,
    description TEXT NOT NULL,
    matched_languages TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_postings_run_city ON postings(run_id, city);
CREATE INDEX IF NOT EXISTS idx_postings_url ON postings(url);
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def record_run(
    conn: sqlite3.Connection,
    *,
    run_at_utc: str,
    since_date: str,
    companies_crawled: int,
    raw_postings_fetched: int,
    failed_companies: int = 0,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO runs (run_at_utc, since_date, companies_crawled, raw_postings_fetched, failed_companies)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_at_utc, since_date, companies_crawled, raw_postings_fetched, failed_companies),
    )
    conn.commit()
    return cursor.lastrowid


def record_city_language_counts(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    city: str,
    total_matched: int,
    ranked: list[dict],
) -> None:
    conn.executemany(
        """
        INSERT INTO city_language_counts (run_id, city, total_matched, language, rank, count, percent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (run_id, city, total_matched, row["language"], row["rank"], row["count"], row["percent"])
            for row in ranked
        ],
    )
    conn.commit()


def record_postings(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    city: str,
    postings: list[dict],
) -> None:
    """Store the full postings (including description text) matched for a
    city in this run, tagged with which candidate languages each one hit.
    Each posting dict must have: company, platform, title, location, url,
    posted_at, description, matched_languages (list[str]).
    """
    conn.executemany(
        """
        INSERT INTO postings
            (run_id, city, company, platform, title, location, url, posted_at, description, matched_languages)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                city,
                posting["company"],
                posting["platform"],
                posting["title"],
                posting["location"],
                posting["url"],
                posting["posted_at"],
                posting["description"],
                json.dumps(posting["matched_languages"]),
            )
            for posting in postings
        ],
    )
    conn.commit()
