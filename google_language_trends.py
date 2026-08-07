#!/usr/bin/env python3
"""
Rank programming languages per city using Google (Serper) for *discovery*,
then the ATS APIs for *full job descriptions* -- the Google counterpart to
language_trends.py, built so the two can be compared apples-to-apples.

How it differs from language_trends.py:
  - language_trends.py crawls a fixed list of tracked companies
    (ats_companies.json) and ranks languages across their postings.
    - This script instead runs one general Google search per city
    ("engineer" | "developer" + the city, restricted to the supported ATS
    domains), which surfaces matching postings from *any* company Google has
    indexed -- including companies not in ats_companies.json.

Why it still needs the ATS APIs: a Google result only carries a ~160-char
snippet, which is far too thin to detect which languages a job wants. So for
each result URL (Lever, Greenhouse, Ashby, or Workday) we fetch the *full* job
description from that platform's public API, then run the
exact same detection as language_trends.py (both import language_detect), so
any difference in the rankings is due to which postings each pipeline finds
(coverage), not how languages are detected (methodology).

Cost note: this uses Serper (like google_job_search.py / discover_ats_companies.py).
It runs a handful of queries per city (one per site-batch, times --max-pages),
NOT one query per language, so it stays cheap. The per-posting description
fetches hit the free ATS APIs, not Serper.

Setup: needs SERPER_API_KEY in .env (same as google_job_search.py).

Output (per run, in --output-dir):
  google_language_trends_{timestamp}.json          -- full structured data, all cities
  google_language_trends_{timestamp}_by_city.csv   -- long format: city, rank, language, count, percent
  google_language_trends_{timestamp}_matrix.csv    -- pivoted: one row per language, one column per city

Each run also updates the same SQLite database language_trends.py uses
(job_trends.db by default, see trends_db.py), tagged source='google' so a
frontend can tell Google-discovery data apart from the ATS-crawl data. The
database keeps ONE combined, always-current dataset that grows INCREMENTALLY:
each run fetches only postings newer than the last time that city was run
(using Google's after: filter seeded from the city's last-fetched date) and
appends just the new ones -- data you already have is never re-fetched. Run it
manually whenever you like (a day or a week apart); each city picks up where it
left off. Postings are deduped by a stable platform/slug/job-id key, so nothing
is stored twice. The full matched postings -- company, title, url, the complete
job description, and which languages each one hit -- are stored in the
`postings` table, so a future UI can let someone click a city (or a language
within a city) and drill into the actual job ads behind the numbers. Pass
--no-postings to keep only the aggregate counts, or --no-db to skip the
database (which also disables incremental tracking -- it becomes a one-shot
fetch from --since-date).

Usage:
  python google_language_trends.py
  python google_language_trends.py --cities "toronto,boston,seattle,dallas"
  python google_language_trends.py --cities dallas --top 5 --insecure
  python google_language_trends.py --no-db   # files only, skip the database
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from ats_job_search import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_SINCE_DATE,
    DEFAULT_TITLE_EXCLUDE,
    DEFAULT_TITLE_INCLUDE,
    fetch_postings_for_urls,
    matches_city,
    matches_city_location,
    matches_role,
    matches_since_date,
    parse_job_url,
    parse_keyword_list,
)
from google_job_search import (
    DEFAULT_ATS_SITES,
    DEFAULT_SITE_BATCH_SIZE,
    FREE_TIER_MAX_RESULTS,
    build_site_queries,
    run_queries,
)
from language_detect import (
    LANGUAGE_KEYWORDS,
    count_from_matched_languages,
    languages_in_posting,
    print_city_report,
    rank_languages,
)
from trends_db import (
    DEFAULT_DB_PATH,
    add_postings,
    ensure_schema,
    get_connection,
    get_last_fetched,
    get_postings,
    posting_is_in_stats_window,
    set_city_counts,
    stats_cutoff_date,
)
from trends_stats import write_stats

# The tracked set of cities (updated together on each run). Edit this list to
# add or drop a city from the ongoing dataset.
DEFAULT_CITIES = (
    "atlanta,austin,boston,charlotte,chicago,dallas,denver,houston,"
    "los angeles,memphis,miami,minneapolis,new york,philadelphia,phoenix,"
    "portland,raleigh,salt lake city,san diego,san francisco,san jose,"
    "seattle,st louis,toronto,washington dc"
)
DEFAULT_TOP = 10
# When resuming a city incrementally, look back a few days before its last-fetched
# date. Google's `after:` filter is date-granular, so a job posted on the boundary
# day but indexed slightly later could otherwise slip through. Dedup by job key
# guarantees the overlap never stores anything twice.
INCREMENTAL_LOOKBACK_DAYS = 3
# The role/city/date filter Google applies at search time. `after:` uses the
# same --since-date value the posting-level filter uses, so both ends agree.
ROLE_FILTER_TEMPLATE = '(engineer | developer) ("{city}") -staff -lead -principal after:{since}'


def build_city_keyword_filter(city: str, since_date_for_query: str) -> str:
    return ROLE_FILTER_TEMPLATE.format(city=city, since=since_date_for_query)


def shift_date_back(date_str: str, days: int) -> str:
    """Return the YYYY-MM-DD `days` before `date_str` (a YYYY-MM-DD string)."""
    parsed = datetime.strptime(date_str, "%Y-%m-%d")
    return (parsed - timedelta(days=days)).strftime("%Y-%m-%d")



def save_results(
    results_by_city: dict[str, dict],
    output_dir: Path,
    *,
    since_date_label: str,
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"google_language_trends_{timestamp}"

    json_path = output_dir / f"{base_name}.json"
    by_city_csv_path = output_dir / f"{base_name}_by_city.csv"
    matrix_csv_path = output_dir / f"{base_name}_matrix.csv"

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "google-discovery + ats-api descriptions",
        "since_date": since_date_label,
        "cities": {
            city: {
                "total_matched": data["total_matched"],
                "new_this_run": data["new_this_run"],
                "languages": data["languages"],
                "postings": data["postings"],
            }
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
            "Rank programming languages per city using Google (Serper) to discover "
            "postings and the ATS APIs to fetch their full descriptions -- the Google "
            "counterpart to language_trends.py, for apples-to-apples comparison."
        )
    )
    parser.add_argument(
        "--cities",
        default=DEFAULT_CITIES,
        help=f"Comma-separated cities to analyze and compare (default: {DEFAULT_CITIES!r})",
    )
    parser.add_argument(
        "--sites",
        default=",".join(DEFAULT_ATS_SITES),
        help="Comma-separated ATS/job-board domains to search (default: the supported set).",
    )
    parser.add_argument(
        "--since-date",
        default=DEFAULT_SINCE_DATE,
        help=(
            f"Floor date for a city's FIRST fetch and the posting-level published-on/after "
            f"check (default: {DEFAULT_SINCE_DATE}). On later runs, each city instead resumes "
            "from its own last-fetched date (incremental), so this only applies to cities not "
            "yet in the database. Pass 'none' to disable the posting-level check."
        ),
    )
    parser.add_argument(
        "--title-include",
        default=DEFAULT_TITLE_INCLUDE,
        help="Comma-separated title phrases a posting must contain (default: the shared SWE list). Pass 'none' to disable.",
    )
    parser.add_argument(
        "--title-exclude",
        default=DEFAULT_TITLE_EXCLUDE,
        help="Comma-separated title phrases that drop a posting (default: manager/director/etc.). Pass 'none' to disable.",
    )
    parser.add_argument(
        "--no-city-filter",
        action="store_true",
        help=(
            "Trust Google's city match and skip re-checking the city against each fetched "
            "posting's location/description. By default the same city check language_trends.py "
            "uses is applied, for parity."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"How many top languages to print per city (default: {DEFAULT_TOP}). Saved files always contain the full ranking.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Max Google result pages to fetch per site-batch query (default: 10).",
    )
    parser.add_argument(
        "--num-per-page",
        type=int,
        default=FREE_TIER_MAX_RESULTS,
        help=f"Google results per page (default: {FREE_TIER_MAX_RESULTS} for free Serper accounts).",
    )
    parser.add_argument(
        "--site-batch-size",
        type=int,
        default=DEFAULT_SITE_BATCH_SIZE,
        help=f"Number of site: domains to OR together per query (default: {DEFAULT_SITE_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"Number of company boards to fetch concurrently (default: {DEFAULT_MAX_WORKERS}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="Directory for output files (default: data).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification (useful on some corporate networks).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=(
            f"SQLite database file to append this run's results to (default: {DEFAULT_DB_PATH.name}, "
            "shared with language_trends.py; Google runs are tagged source='google'). "
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
    load_dotenv(override=True)
    args = parse_args()

    api_key = os.getenv("SERPER_API_KEY")
    if not api_key:
        print(
            "Error: SERPER_API_KEY is not set.\n"
            "Get a free key at https://serper.dev and add it to your .env file:\n"
            "  SERPER_API_KEY=your_key_here",
            file=sys.stderr,
        )
        return 1

    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    if not cities:
        print("Error: --cities must contain at least one city", file=sys.stderr)
        return 1

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
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
    since_for_query = since_date_label if since_date is not None else "2000-01-01"

    title_include = parse_keyword_list(args.title_include)
    title_exclude = parse_keyword_list(args.title_exclude)

    conn = None
    if not args.no_db:
        conn = get_connection(args.db_path)
        ensure_schema(conn)

    results_by_city: dict[str, dict] = {}

    for city in cities:
        # Incremental: only look for postings newer than the last time this city
        # was fetched, so we never re-fetch data we already have. The first time
        # a city is seen (or with --no-db), fall back to the --since-date floor.
        last_fetched = get_last_fetched(conn, city=city, source="google") if conn is not None else None
        since_for_city = (
            shift_date_back(last_fetched[:10], INCREMENTAL_LOOKBACK_DAYS)
            if last_fetched
            else since_for_query
        )

        existing = get_postings(conn, city=city, source="google") if conn is not None else []
        existing_keys = {
            key for p in existing if (key := parse_job_url(p.get("url", ""))) is not None
        }

        keyword_filter = build_city_keyword_filter(city, since_for_city)
        queries = build_site_queries(sites, keyword_filter, batch_size=args.site_batch_size)
        window = f"since {since_for_city}" if last_fetched else f"from {since_for_city} (first fetch)"
        print(f"\n[{city}] running {len(queries)} Google query(ies) for postings {window}...")

        results, exit_code = run_queries(
            queries,
            api_key,
            max_pages=args.max_pages,
            num_per_page=args.num_per_page,
            verify=verify,
        )
        if results is None:
            if conn is not None:
                conn.close()
            return exit_code or 1

        discovered = [r["link"] for r in results if r.get("link")]
        # Skip URLs we already have stored (deduped by platform/slug/job-id key,
        # which is stable across host/`/apply` variations) so we don't re-fetch them.
        to_fetch = [
            url
            for url in discovered
            if (key := parse_job_url(url)) is not None and key not in existing_keys
        ]
        already_have = sum(
            1 for url in discovered if (key := parse_job_url(url)) is not None and key in existing_keys
        )
        print(
            f"[{city}] discovered {len(discovered)} result URL(s); "
            f"{already_have} already stored, fetching {len(to_fetch)} new..."
        )

        fetched, unresolved, errors = fetch_postings_for_urls(
            to_fetch, verify=verify, max_workers=args.max_workers
        )
        if errors:
            print(f"[{city}] {len(errors)} board(s) failed to fetch and were skipped:", file=sys.stderr)
            for err in errors:
                print(f"    - {err}", file=sys.stderr)
        if unresolved:
            print(f"[{city}] {len(unresolved)} new URL(s) couldn't be resolved to a live posting (skipped).")

        new_filtered = [
            p
            for p in fetched
            if matches_role(p, include_keywords=title_include, exclude_keywords=title_exclude)
            and matches_since_date(p, since_date)
            and (
                args.no_city_filter
                or (
                    matches_city_location(p, city)
                    if p.get("platform") == "workday"
                    else matches_city(p, city)
                )
            )
        ]
        new_with_langs = [
            {**p, "matched_languages": languages_in_posting(p)} for p in new_filtered
        ]

        # Persist every accepted posting for history/deduplication, but rank only
        # the rolling six-month window. Older rows remain stored indefinitely.
        if conn is not None:
            if not args.no_postings:
                add_postings(conn, city=city, source="google", postings=new_with_langs)

        cutoff = stats_cutoff_date()
        recent_postings = [
            p for p in (existing + new_with_langs) if posting_is_in_stats_window(p, cutoff)
        ]
        combined_matched_lists = [p["matched_languages"] for p in recent_postings]
        total_matched = len(recent_postings)
        counts = count_from_matched_languages(combined_matched_lists)
        ranked = rank_languages(counts, total_matched)

        if conn is not None:
            set_city_counts(
                conn,
                city=city,
                source="google",
                since_date=since_date_label,
                total_matched=total_matched,
                ranked=ranked,
            )

        combined_postings = [
            {
                "company": p.get("company", ""),
                "platform": p.get("platform", ""),
                "title": p.get("title", ""),
                "location": p.get("location", ""),
                "url": p.get("url", ""),
                "posted_at": p.get("posted_at", ""),
                "matched_languages": p["matched_languages"],
            }
            for p in recent_postings
        ]
        results_by_city[city] = {
            "total_matched": total_matched,
            "new_this_run": len(new_with_langs),
            "languages": ranked,
            "languages_by_name": counts,
            "postings": combined_postings,
        }
        stored_total = len(existing) + len(new_with_langs)
        print(
            f"[{city}] +{len(new_with_langs)} new, {total_matched} in past 6 months "
            f"({stored_total} total stored)."
        )
        print_city_report(city, total_matched, ranked, args.top)

    if conn is not None:
        stats_txt, stats_json = write_stats(
            conn,
            out_dir=args.output_dir,
            source="google",
            top=args.top,
            db_path=str(args.db_path),
        )
        conn.close()
        print(
            f"\nUpdated {len(cities)} cit{'y' if len(cities) == 1 else 'ies'} "
            f"(source='google') in the combined SQLite dataset: {args.db_path}"
        )
        print(f"Refreshed running stats snapshot:\n  {stats_txt}\n  {stats_json}")

    json_path, by_city_csv_path, matrix_csv_path = save_results(
        results_by_city, args.output_dir, since_date_label=since_date_label
    )
    print("\nSaved Google-discovery language ranking (all candidate languages, every city):")
    print(f"  JSON:        {json_path}")
    print(f"  By-city CSV: {by_city_csv_path}")
    print(f"  Matrix CSV:  {matrix_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
