#!/usr/bin/env python3
"""
Analyze which programming languages are in demand, ranked per city, by
crawling the same ATS APIs as ats_job_search.py (Greenhouse/Lever/Ashby --
no Google/Serper, no query quota).

Unlike ats_job_search.py's --languages flag (which *filters* postings down
to a fixed set of 5 languages you choose), this script does not restrict
which languages can show up. It scans every matched posting against a
broad, hard-coded list of ~24 candidate languages (so we don't miss
anything that could plausibly appear in a city's top 5/10), counts how
many postings mention each one, and reports the full ranking -- so you can
see the *actual* top 5 or top 10 for a city, and compare across cities
(e.g. is Java still big in Dallas but not in San Francisco?).

The candidate language list is intentionally generous. Growing it just
means scanning for a few more substrings -- it never reduces what postings
get returned, unlike ats_job_search.py's language filter which drops
postings that don't match.

Usage:
  python language_trends.py
  python language_trends.py --cities "dallas,san francisco,toronto,new york"
  python language_trends.py --cities austin --top 5
  python language_trends.py --insecure   # corporate VPN/proxy SSL issues
  python language_trends.py --no-db       # skip the SQLite database, files only

Output (per run, in --output-dir):
  language_trends_{timestamp}.json          -- full structured data, all cities
  language_trends_{timestamp}_by_city.csv   -- long format: city, rank, language, count, percent
  language_trends_{timestamp}_matrix.csv    -- pivoted: one row per language, one column per city

Each run is also appended to a local SQLite database (job_trends.db by
default, see trends_db.py) so results can be compared over time -- run this
script on a cron schedule to build up a real trend history instead of a
one-off snapshot. Alongside the aggregate counts, the full matched postings
(company, title, url, description, and which languages each one hit) are
also stored in the database's `postings` table, so a future UI can let
someone click a city -- or a specific language within a city -- and drill
into the actual job ads behind the numbers. Pass --no-postings to skip that
and only keep the smaller aggregate-count history.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import urllib3

from ats_job_search import (
    DEFAULT_COMPANIES_FILE,
    DEFAULT_MAX_WORKERS,
    DEFAULT_SINCE_DATE,
    DEFAULT_TITLE_EXCLUDE,
    DEFAULT_TITLE_INCLUDE,
    collect_all_postings,
    load_companies,
    matches_city,
    matches_role,
    matches_since_date,
    parse_keyword_list,
)
from trends_db import (
    DEFAULT_DB_PATH,
    ensure_schema,
    get_connection,
    record_city_language_counts,
    record_postings,
    record_run,
)

DEFAULT_CITIES = "toronto,dallas,san francisco"
DEFAULT_TOP = 10

# Broad candidate list of languages to detect and rank -- not a filter.
# Growing this list costs nothing (it never excludes postings), so it's
# deliberately generous to avoid missing a language that turns out to be
# in a city's top 5/10.
#
# A few single-token names (Go, R, C) are common English words too, so
# instead of matching them bare (which would drown in false positives like
# "we go the extra mile" or "Series C"), we match on safer, more specific
# phrases. That trades some recall for much better precision -- consistent
# with "good enough for market signal, not 100% recall".
LANGUAGE_KEYWORDS: dict[str, list[str]] = {
    "JavaScript/TypeScript": ["javascript", "typescript"],
    "Python": ["python"],
    "Java": ["java"],
    "C#": ["c#"],
    "C++": ["c++"],
    "Go": ["golang", "go programming", "go developer", "go engineer"],
    "Rust": ["rust"],
    "Ruby": ["ruby"],
    "PHP": ["php"],
    "Swift": ["swift"],
    "Kotlin": ["kotlin"],
    "Scala": ["scala"],
    "R": ["rstudio", "r programming", "r language", "tidyverse"],
    "Perl": ["perl"],
    "Objective-C": ["objective-c", "objective c"],
    "Dart": ["dart"],
    "Elixir": ["elixir"],
    "Haskell": ["haskell"],
    "Lua": ["lua"],
    "Shell/Bash": ["bash", "shell scripting", "shell script"],
    "MATLAB": ["matlab"],
    "Groovy": ["groovy"],
    "SQL": ["sql"],
}

# Phrases containing regex-special or too-short-for-\b characters get a
# plain substring check instead of a word-boundary regex.
SUBSTRING_ONLY_PHRASES = {"c#", "c++"}


def phrase_in_text(phrase: str, lowercase_haystack: str) -> bool:
    if phrase in SUBSTRING_ONLY_PHRASES:
        return phrase in lowercase_haystack
    return re.search(rf"\b{re.escape(phrase)}\b", lowercase_haystack) is not None


def languages_in_posting(posting: dict) -> list[str]:
    """Return the list of candidate languages detected in one posting."""
    haystack = f"{posting['title']} {posting['description']}".lower()
    return [
        lang
        for lang, phrases in LANGUAGE_KEYWORDS.items()
        if any(phrase_in_text(phrase, haystack) for phrase in phrases)
    ]


def count_languages(postings: list[dict]) -> dict[str, int]:
    counts = {lang: 0 for lang in LANGUAGE_KEYWORDS}
    for posting in postings:
        for lang in languages_in_posting(posting):
            counts[lang] += 1
    return counts


def rank_languages(counts: dict[str, int], total_matched: int) -> list[dict]:
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked = []
    for i, (lang, count) in enumerate(ordered):
        percent = round(100 * count / total_matched, 1) if total_matched else 0.0
        ranked.append({"rank": i + 1, "language": lang, "count": count, "percent": percent})
    return ranked


def print_city_report(city: str, total_matched: int, ranked: list[dict], top: int) -> None:
    print(f"\n=== {city.title()} ({total_matched} matched posting(s)) ===")
    if total_matched == 0:
        print("  (no postings matched this city)")
        return
    shown = [row for row in ranked if row["count"] > 0][:top]
    if not shown:
        print("  (no candidate languages detected in matched postings)")
        return
    for row in shown:
        print(f"  {row['rank']:>2}. {row['language']:<12} {row['count']:>4}  ({row['percent']}%)")


def save_results(
    results_by_city: dict[str, dict],
    output_dir: Path,
    *,
    since_date_label: str,
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"language_trends_{timestamp}"

    json_path = output_dir / f"{base_name}.json"
    by_city_csv_path = output_dir / f"{base_name}_by_city.csv"
    matrix_csv_path = output_dir / f"{base_name}_matrix.csv"

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "since_date": since_date_label,
        "cities": {
            city: {"total_matched": data["total_matched"], "languages": data["languages"]}
            for city, data in results_by_city.items()
        },
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    with by_city_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["city", "rank", "language", "count", "percent"])
        writer.writeheader()
        for city, data in results_by_city.items():
            for row in data["languages"]:
                writer.writerow({"city": city, **row})

    cities = list(results_by_city.keys())
    totals = {lang: sum(results_by_city[c]["languages_by_name"][lang] for c in cities) for lang in LANGUAGE_KEYWORDS}
    languages_sorted = sorted(LANGUAGE_KEYWORDS, key=lambda lang: -totals[lang])
    with matrix_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["language", *cities, "total"])
        for lang in languages_sorted:
            row_counts = [results_by_city[c]["languages_by_name"][lang] for c in cities]
            writer.writerow([lang, *row_counts, totals[lang]])

    return json_path, by_city_csv_path, matrix_csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rank programming languages by job-posting demand per city, by crawling "
            "ATS platforms' free public APIs directly (no Google/Serper, no quota)."
        )
    )
    parser.add_argument(
        "--companies-file",
        type=Path,
        default=DEFAULT_COMPANIES_FILE,
        help=f"JSON file listing companies to crawl (default: {DEFAULT_COMPANIES_FILE.name})",
    )
    parser.add_argument(
        "--cities",
        default=DEFAULT_CITIES,
        help=f"Comma-separated cities to analyze and compare (default: {DEFAULT_CITIES!r})",
    )
    parser.add_argument(
        "--since-date",
        default=DEFAULT_SINCE_DATE,
        help=(
            f"Only include postings first published on/after this date "
            f"(default: {DEFAULT_SINCE_DATE}). Pass 'none' to disable."
        ),
    )
    parser.add_argument(
        "--title-include",
        default=DEFAULT_TITLE_INCLUDE,
        help=(
            "Comma-separated phrase(s) -- a posting's title must contain at least one "
            f"(case-insensitive) to be kept (default: {DEFAULT_TITLE_INCLUDE!r}). "
            "Pass 'none' to keep postings regardless of title."
        ),
    )
    parser.add_argument(
        "--title-exclude",
        default=DEFAULT_TITLE_EXCLUDE,
        help=(
            "Comma-separated phrase(s) -- a posting is dropped if its title contains any of "
            f"these (case-insensitive), even if it matched --title-include (default: "
            f"{DEFAULT_TITLE_EXCLUDE!r}). Pass 'none' to disable."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=(
            f"How many top languages to print per city on the console (default: {DEFAULT_TOP}). "
            "This only affects console output -- saved files always contain the full ranking "
            "of every candidate language, so nothing is hidden or hard-limited."
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"Number of companies to fetch concurrently (default: {DEFAULT_MAX_WORKERS})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="Directory for output files (default: data)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification (useful on some corporate networks)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=(
            f"SQLite database file to append this run's results to (default: {DEFAULT_DB_PATH.name}). "
            "Created automatically if it doesn't exist yet."
        ),
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip writing results to the SQLite database (only save the CSV/JSON files).",
    )
    parser.add_argument(
        "--no-postings",
        action="store_true",
        help=(
            "Skip storing full job postings/descriptions in the database (the 'postings' "
            "table). Aggregate language counts are still recorded. Use this to keep the "
            "database smaller if you only care about the counts, not drill-down detail."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.companies_file.exists():
        print(f"Error: companies file not found: {args.companies_file}", file=sys.stderr)
        return 1

    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    if not cities:
        print("Error: --cities must contain at least one city", file=sys.stderr)
        return 1

    companies = load_companies(args.companies_file)
    verify = not args.insecure
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    since_date = None
    since_date_label = "none"
    if args.since_date and args.since_date.strip().lower() != "none":
        try:
            since_date = datetime.fromisoformat(args.since_date).replace(tzinfo=timezone.utc)
            since_date_label = args.since_date
        except ValueError:
            print(f"Error: --since-date must be YYYY-MM-DD, got {args.since_date!r}", file=sys.stderr)
            return 1

    title_include = parse_keyword_list(args.title_include)
    title_exclude = parse_keyword_list(args.title_exclude)

    print(
        f"Crawling {len(companies)} compan{'y' if len(companies) == 1 else 'ies'} "
        f"({args.max_workers} at a time)..."
    )
    all_postings, errors = collect_all_postings(companies, verify=verify, max_workers=args.max_workers)
    print(f"Fetched {len(all_postings)} raw posting(s) from {len(companies)} companies.")
    if errors:
        print(f"  ({len(errors)} companies failed to fetch and were skipped)")
        for err in errors:
            print(f"    - {err}", file=sys.stderr)
        if verify and any("CERTIFICATE_VERIFY_FAILED" in err for err in errors):
            print(
                "Tip: some failures were SSL certificate errors -- retry with "
                "--insecure if you're on a corporate VPN/proxy.",
                file=sys.stderr,
            )

    results_by_city: dict[str, dict] = {}
    run_id = None
    conn = None
    if not args.no_db:
        conn = get_connection(args.db_path)
        ensure_schema(conn)
        run_id = record_run(
            conn,
            run_at_utc=datetime.now(timezone.utc).isoformat(),
            since_date=since_date_label,
            companies_crawled=len(companies),
            raw_postings_fetched=len(all_postings),
            failed_companies=len(errors),
        )

    for city in cities:
        filtered = [
            p
            for p in all_postings
            if matches_city(p, city)
            and matches_since_date(p, since_date)
            and matches_role(p, include_keywords=title_include, exclude_keywords=title_exclude)
        ]
        counts = count_languages(filtered)
        ranked = rank_languages(counts, len(filtered))
        results_by_city[city] = {
            "total_matched": len(filtered),
            "languages": ranked,
            "languages_by_name": counts,
        }
        print_city_report(city, len(filtered), ranked, args.top)
        if run_id is not None:
            record_city_language_counts(conn, run_id=run_id, city=city, total_matched=len(filtered), ranked=ranked)
            if not args.no_postings:
                postings_with_langs = [
                    {**posting, "matched_languages": languages_in_posting(posting)} for posting in filtered
                ]
                record_postings(conn, run_id=run_id, city=city, postings=postings_with_langs)

    if conn is not None:
        conn.close()
        print(f"\nRecorded snapshot run #{run_id} to SQLite database: {args.db_path}")

    json_path, by_city_csv_path, matrix_csv_path = save_results(
        results_by_city, args.output_dir, since_date_label=since_date_label
    )
    print(f"\nSaved full ranking (all {len(LANGUAGE_KEYWORDS)} candidate languages, every city):")
    print(f"  JSON:        {json_path}")
    print(f"  By-city CSV: {by_city_csv_path}")
    print(f"  Matrix CSV:  {matrix_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
