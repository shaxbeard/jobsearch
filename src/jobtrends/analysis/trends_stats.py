#!/usr/bin/env python3
"""Quick running-statistics report over the combined job-trends database.

Reads job_trends.db (see trends_db.py) and prints, for the accumulated data:
  * a one-line summary (postings, cities, last update) per source,
  * the top languages in each city,
  * the top languages overall (all cities combined),
  * the top job titles overall.

Everything is computed from the `postings` table's per-posting matched_languages
(the ground truth), ranked the same way the pipelines rank, so the numbers here
always agree with what the fetchers store -- and --source all can blend the
Google and ATS data without double-ranking.

The fetchers (e.g. google_language_trends.py) call write_stats() at the end of
every run, refreshing a stable, always-current snapshot you can open instantly:
  data/latest_stats.txt   -- the human-readable report below
  data/latest_stats.json  -- the same numbers as structured data (for a UI)

Usage:
  python trends_stats.py                 # print google stats (default)
  python trends_stats.py --source ats
  python trends_stats.py --source all    # google + ats combined
  python trends_stats.py --top 5 --titles 20
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jobtrends.language_detect import count_from_matched_languages, rank_languages
from jobtrends.trends_db import (
    DEFAULT_DB_PATH,
    STATS_WINDOW_MONTHS,
    ensure_schema,
    get_connection,
    stats_cutoff_date,
)

# Stable filenames refreshed on every fetch run (see write_stats).
LATEST_STATS_TXT = "latest_stats.txt"
LATEST_STATS_JSON = "latest_stats.json"


def load_postings(
    conn,
    source: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Return in-window postings with matched_languages decoded to a list."""
    start_date = start_date or stats_cutoff_date()
    end_date = end_date or datetime.now(timezone.utc).date().isoformat()
    if source == "all":
        rows = conn.execute(
            "SELECT city, source, title, posted_at, matched_languages FROM postings "
            "WHERE date(posted_at) BETWEEN date(?) AND date(?)",
            (start_date, end_date),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT city, source, title, posted_at, matched_languages FROM postings "
            "WHERE source = ? AND date(posted_at) BETWEEN date(?) AND date(?)",
            (source, start_date, end_date),
        ).fetchall()

    postings = []
    for city, src, title, posted_at, matched_json in rows:
        try:
            langs = json.loads(matched_json) if matched_json else []
        except (TypeError, json.JSONDecodeError):
            langs = []
        postings.append(
            {
                "city": city,
                "source": src,
                "title": title or "",
                "posted_at": posted_at or "",
                "matched_languages": langs,
            }
        )
    return postings


def build_daily_posting_counts(
    postings: list[dict],
    *,
    cutoff: str | None = None,
    through: date | None = None,
) -> list[dict]:
    """Return a zero-filled daily series based on ATS-advertised posting dates."""
    first_day = date.fromisoformat(cutoff or stats_cutoff_date())
    last_day = through or datetime.now(timezone.utc).date()
    counts = Counter(
        str(posting.get("posted_at") or "")[:10]
        for posting in postings
        if len(str(posting.get("posted_at") or "")) >= 10
    )
    days = (last_day - first_day).days
    return [
        {
            "date": (first_day + timedelta(days=offset)).isoformat(),
            "count": counts[(first_day + timedelta(days=offset)).isoformat()],
        }
        for offset in range(max(days + 1, 0))
    ]


def _rank(postings: list[dict], top: int) -> list[dict]:
    """Ranked, nonzero languages for a set of postings (top N)."""
    total = len(postings)
    if total == 0:
        return []
    counts = count_from_matched_languages([p["matched_languages"] for p in postings])
    ranked = rank_languages(counts, total)
    return [row for row in ranked if row["count"] > 0][:top]


def build_stats_data(
    conn,
    *,
    source: str,
    top: int,
    titles: int,
    start_date: str | None = None,
    end_date: str | None = None,
    range_key: str = "6m",
    range_label: str = "Past 6 months",
) -> dict:
    """Structured running statistics (used for both the text report and JSON)."""
    cutoff = start_date or stats_cutoff_date()
    through = end_date or datetime.now(timezone.utc).date().isoformat()
    summary = conn.execute(
        "SELECT source, COUNT(DISTINCT city), COUNT(*), MAX(updated_at_utc) "
        "FROM postings WHERE date(posted_at) BETWEEN date(?) AND date(?) "
        "GROUP BY source ORDER BY source",
        (cutoff, through),
    ).fetchall()
    by_source = [
        {"source": src, "cities": n_cities, "postings": n_postings, "last_update": last}
        for src, n_cities, n_postings, last in summary
    ]

    postings = load_postings(conn, source, start_date=cutoff, end_date=through)
    cities = sorted({p["city"] for p in postings})
    per_city = [
        {"city": city, "postings": len([p for p in postings if p["city"] == city]),
         "languages": _rank([p for p in postings if p["city"] == city], top)}
        for city in cities
    ]
    title_counts = Counter(p["title"].strip() for p in postings if p["title"].strip())

    return {
        "source": source,
        "window_months": STATS_WINDOW_MONTHS,
        "range_key": range_key,
        "range_label": range_label,
        "start_date": cutoff,
        "end_date": through,
        "cutoff_date": cutoff,
        "total_postings": len(postings),
        "posting_activity_daily": build_daily_posting_counts(
            postings, cutoff=cutoff, through=date.fromisoformat(through)
        ),
        "summary_by_source": by_source,
        "top_languages_per_city": per_city,
        "top_languages_overall": _rank(postings, top),
        "top_titles_overall": [
            {"title": title, "count": count}
            for title, count in title_counts.most_common(titles)
        ],
    }


def build_report(conn, *, source: str, top: int, titles: int, db_path: str = "") -> str:
    """Human-readable running-statistics report as a single string."""
    data = build_stats_data(conn, source=source, top=top, titles=titles)
    lines: list[str] = []
    if db_path:
        lines.append(f"Database: {db_path}")

    if not data["summary_by_source"]:
        lines.append("(no postings stored yet)")
        return "\n".join(lines)

    lines.append("\nData in stats window by source:")
    for s in data["summary_by_source"]:
        lines.append(
            f"  {s['source']:<8} {s['postings']:>4} postings across "
            f"{s['cities']:>2} cities  (last update {s['last_update']})"
        )

    header = f"source={source}, past {STATS_WINDOW_MONTHS} months"
    if data["total_postings"] == 0:
        lines.append(f"\nNo postings for source={source!r}.")
        return "\n".join(lines)

    def render_ranking(label: str, total: int, ranked: list[dict]) -> None:
        lines.append(f"\n=== {label} ({total} postings) ===")
        if not ranked:
            lines.append("  (no candidate languages detected)")
            return
        for row in ranked:
            lines.append(
                f"  {row['rank']:>2}. {row['language']:<22} {row['count']:>4}  ({row['percent']}%)"
            )

    lines.append(f"\n{'-' * 60}\nTOP LANGUAGES PER CITY ({header})\n{'-' * 60}")
    for city in data["top_languages_per_city"]:
        render_ranking(city["city"].title(), city["postings"], city["languages"])

    lines.append(f"\n{'-' * 60}\nTOP LANGUAGES OVERALL ({header})\n{'-' * 60}")
    render_ranking("All cities combined", data["total_postings"], data["top_languages_overall"])

    lines.append(f"\n{'-' * 60}\nTOP {titles} JOB TITLES OVERALL ({header})\n{'-' * 60}")
    for rank, t in enumerate(data["top_titles_overall"], start=1):
        lines.append(f"  {rank:>2}. {t['count']:>3}x  {t['title']}")

    return "\n".join(lines)


def write_stats(
    conn,
    *,
    out_dir: Path,
    source: str = "google",
    top: int = 10,
    titles: int = 10,
    db_path: str = "",
) -> tuple[Path, Path]:
    """Refresh the stable latest_stats.{txt,json} snapshot and return their paths.

    Called by the fetchers at the end of a run so the running stats always
    reflect the newest data without piling up timestamped files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / LATEST_STATS_TXT
    json_path = out_dir / LATEST_STATS_JSON

    report = build_report(conn, source=source, top=top, titles=titles, db_path=db_path)
    data = build_stats_data(conn, source=source, top=top, titles=titles)

    txt_path.write_text(report + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return txt_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source",
        default="google",
        choices=["google", "ats", "all"],
        help="Which data to report on (default: google).",
    )
    parser.add_argument("--top", type=int, default=10, help="Top N languages per city / overall (default: 10).")
    parser.add_argument("--titles", type=int, default=10, help="Top N job titles overall (default: 10).")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to the SQLite database.")
    args = parser.parse_args()

    conn = get_connection(Path(args.db_path))
    ensure_schema(conn)
    try:
        print(build_report(conn, source=args.source, top=args.top, titles=args.titles, db_path=args.db_path))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
