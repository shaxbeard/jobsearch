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

Each run also updates a local SQLite database (job_trends.db by default, see
trends_db.py) in place -- keeping ONE combined, always-current dataset rather
than a growing history of separate runs. Re-running a city replaces that
city's rows, so you can refresh cities independently and still query a single
unified dataset. Alongside the aggregate counts, the full matched postings
(company, title, url, description, and which languages each one hit) are also
stored in the database's `postings` table, so a future UI can let someone
click a city -- or a specific language within a city -- and drill into the
actual job ads behind the numbers. Pass --no-postings to skip that and only
keep the smaller aggregate-count data.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import urllib3

from jobtrends.paths import DATA_DIR
from jobtrends.sources.ats_job_search import (
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
from jobtrends.language_detect import (
    LANGUAGE_KEYWORDS,
    count_languages,
    languages_in_posting,
    print_city_report,
    rank_languages,
)
from jobtrends.trends_db import (
    DEFAULT_DB_PATH,
    ensure_schema,
    get_connection,
    update_city,
)

DEFAULT_CITIES = "toronto,dallas,san francisco"
DEFAULT_TOP = 10


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
        default=DATA_DIR,
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
    conn = None
    if not args.no_db:
        conn = get_connection(args.db_path)
        ensure_schema(conn)

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
        if conn is not None:
            postings_with_langs = None
            if not args.no_postings:
                postings_with_langs = [
                    {**posting, "matched_languages": languages_in_posting(posting)} for posting in filtered
                ]
            update_city(
                conn,
                city=city,
                source="ats",
                since_date=since_date_label,
                total_matched=len(filtered),
                ranked=ranked,
                postings=postings_with_langs,
                store_postings=not args.no_postings,
            )

    if conn is not None:
        conn.close()
        print(
            f"\nUpdated {len(cities)} cit{'y' if len(cities) == 1 else 'ies'} "
            f"(source='ats') in the combined SQLite dataset: {args.db_path}"
        )

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
