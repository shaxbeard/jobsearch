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
each result URL (Lever, Greenhouse, Ashby, Workday, or SmartRecruiters) we fetch the *full* job
description from that platform's public API, then run the
exact same detection as language_trends.py (both import language_detect), so
any difference in the rankings is due to which postings each pipeline finds
(coverage), not how languages are detected (methodology).

Cost note: this uses Serper (like google_job_search.py / discover_ats_companies.py).
It runs a handful of queries per city (one per site-batch, times --max-pages),
NOT one query per language, so it stays cheap. The per-posting description
fetches hit the free ATS APIs, not Serper.

Setup: needs SERPER_API_KEY in .env (same as google_job_search.py).

Each run updates the same SQLite database language_trends.py uses
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
fetch from --since-date with a console report only).

Usage:
  python google_language_trends.py
  python google_language_trends.py --cities "toronto,boston,seattle,dallas"
  python google_language_trends.py --cities dallas --top 5 --insecure
  python google_language_trends.py --no-db   # console report only, skip the database
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from jobtrends.sources.ats_job_search import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_SINCE_DATE,
    DEFAULT_TITLE_EXCLUDE,
    DEFAULT_TITLE_INCLUDE,
    fetch_postings_for_urls,
    city_search_terms,
    matches_city_location,
    matches_role,
    matches_since_date,
    parse_job_url,
    parse_keyword_list,
)
from jobtrends.sources.google_job_search import (
    DEFAULT_ATS_SITES,
    DEFAULT_SITE_BATCH_SIZE,
    FREE_TIER_MAX_RESULTS,
    PAID_TIER_MAX_RESULTS,
    build_site_queries,
    run_queries,
)
from jobtrends.language_detect import (
    count_from_matched_languages,
    languages_in_posting,
    print_city_report,
    rank_languages,
    tools_in_posting,
)
from jobtrends.trends_db import (
    DEFAULT_DB_PATH,
    add_postings,
    count_postings,
    ensure_schema,
    get_connection,
    get_last_fetched,
    get_posting_urls,
    get_recent_postings_summary,
    posting_is_in_stats_window,
    set_city_counts,
    stats_cutoff_date,
)

# The tracked set of cities (updated together on each run). Edit this list to
# add or drop a city from the ongoing dataset.
DEFAULT_CITIES = (
    "atlanta,austin,boston,chicago,dallas,denver,detroit,houston,"
    "los angeles,miami,minneapolis,montreal,new york,philadelphia,phoenix,"
    "portland,raleigh,salt lake city,san diego,san francisco,san jose,"
    "seattle,tampa,toronto,vancouver,washington dc"
)
DEFAULT_TOP = 10
# When resuming a city incrementally, look back a few days before its last-fetched
# date. Google's `after:` filter is date-granular, so a job posted on the boundary
# day but indexed slightly later could otherwise slip through. Dedup by job key
# guarantees the overlap never stores anything twice.
INCREMENTAL_LOOKBACK_DAYS = 3
# The role/city/date filter Google applies at search time. `after:` uses the
# same --since-date value the posting-level filter uses, so both ends agree.
ROLE_FILTER_TEMPLATE = '(engineer | developer) ({locations}) -staff -lead -principal after:{since}'
ROLE_FILTER_TEMPLATE_NO_DATE = '(engineer | developer) ({locations}) -staff -lead -principal'

# Sites whose `site:` query is run WITHOUT Google's `after:` date filter.
# Google's `after:` operator filters on the date *it* associates with the
# indexed page, not on when the underlying job was posted -- and some
# client-rendered career-site products (e.g. careerpuck.com, a white-labeled
# front end some Greenhouse customers use) apparently don't give Google a
# fresh/reliable page date, so `after:<recent date>` silently returns zero
# results for them even though older, still-open postings are indexed and
# would otherwise match. Safe to omit `after:` for these sites: postings
# already stored are still skipped via URL-key dedup, so this just costs one
# extra Serper query per city per run, not duplicate storage or re-work.
# Currently empty: careerpuck.com was removed from DEFAULT_ATS_SITES (low
# yield), which is what this set existed to work around. Re-add a domain
# here if it's re-enabled and hits the same after: bug.
NO_DATE_FILTER_SITES = frozenset()


def build_city_keyword_filter(
    city: str, since_date_for_query: str, *, include_date: bool = True
) -> str:
    locations = " | ".join(f'"{term}"' for term in city_search_terms(city))
    if not include_date:
        return ROLE_FILTER_TEMPLATE_NO_DATE.format(locations=locations)
    return ROLE_FILTER_TEMPLATE.format(locations=locations, since=since_date_for_query)


def shift_date_back(date_str: str, days: int) -> str:
    """Return the YYYY-MM-DD `days` before `date_str` (a YYYY-MM-DD string)."""
    parsed = datetime.strptime(date_str, "%Y-%m-%d")
    return (parsed - timedelta(days=days)).strftime("%Y-%m-%d")


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
        default=None,
        help=(
            "Max Google result pages to fetch per site-batch query per city "
            "(default: 2 with --paid, 10 on the free tier). Pass explicitly to override."
        ),
    )
    parser.add_argument(
        "--num-per-page",
        type=int,
        default=None,
        help=(
            f"Google results per page (default: {PAID_TIER_MAX_RESULTS} with --paid, "
            f"{FREE_TIER_MAX_RESULTS} on the free tier)."
        ),
    )
    parser.add_argument(
        "--paid",
        action="store_true",
        help="Use paid Serper limits (100 results per page, 2 pages by default instead of 10).",
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
        help="Skip writing results to the SQLite database (console report only).",
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


def _peak_rss_mb() -> float:
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


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

    max_results = PAID_TIER_MAX_RESULTS if args.paid else FREE_TIER_MAX_RESULTS
    requested_num_per_page = args.num_per_page if args.num_per_page is not None else max_results
    num_per_page = max(1, min(requested_num_per_page, max_results))
    if not args.paid and requested_num_per_page > FREE_TIER_MAX_RESULTS:
        print(
            f"Note: free Serper accounts are limited to {FREE_TIER_MAX_RESULTS} results "
            f"per request; using {num_per_page}.",
        )
    max_pages = args.max_pages if args.max_pages is not None else (2 if args.paid else 10)

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

        existing_urls = get_posting_urls(conn, city=city, source="google") if conn is not None else set()
        existing_keys = {
            key for url in existing_urls if (key := parse_job_url(url)) is not None
        }
        existing_count = count_postings(conn, city=city, source="google") if conn is not None else 0

        dated_sites = [site for site in sites if site not in NO_DATE_FILTER_SITES]
        undated_sites = [site for site in sites if site in NO_DATE_FILTER_SITES]

        keyword_filter = build_city_keyword_filter(city, since_for_city)
        queries = build_site_queries(dated_sites, keyword_filter, batch_size=args.site_batch_size)
        if undated_sites:
            keyword_filter_no_date = build_city_keyword_filter(city, since_for_city, include_date=False)
            queries += build_site_queries(
                undated_sites, keyword_filter_no_date, batch_size=args.site_batch_size
            )
        window = f"since {since_for_city}" if last_fetched else f"from {since_for_city} (first fetch)"
        print(f"\n[{city}] running {len(queries)} Google query(ies) for postings {window}...")

        results, exit_code = run_queries(
            queries,
            api_key,
            max_pages=max_pages,
            num_per_page=num_per_page,
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
                or matches_city_location(p, city)
            )
        ]
        new_with_langs = [
            {**p, "matched_languages": languages_in_posting(p), "matched_tools": tools_in_posting(p)}
            for p in new_filtered
        ]

        # Read the window BEFORE storing new postings so they aren't counted twice.
        cutoff = stats_cutoff_date()
        existing_recent = (
            get_recent_postings_summary(conn, city=city, source="google", since_date=cutoff)
            if conn is not None
            else []
        )
        new_recent = [p for p in new_with_langs if posting_is_in_stats_window(p, cutoff)]
        recent_postings = existing_recent + new_recent
        combined_matched_lists = [p["matched_languages"] for p in recent_postings]
        total_matched = len(recent_postings)
        counts = count_from_matched_languages(combined_matched_lists)
        ranked = rank_languages(counts, total_matched)

        # Persist every accepted posting for history/deduplication, but rank only
        # the rolling six-month window. Older rows remain stored indefinitely.
        if conn is not None:
            if not args.no_postings:
                add_postings(conn, city=city, source="google", postings=new_with_langs)
            set_city_counts(
                conn,
                city=city,
                source="google",
                since_date=since_date_label,
                total_matched=total_matched,
                ranked=ranked,
            )

        stored_total = existing_count + len(new_with_langs)
        print(
            f"[{city}] +{len(new_with_langs)} new, {total_matched} in past 6 months "
            f"({stored_total} total stored); peak memory {_peak_rss_mb():.0f} MB.",
            flush=True,
        )
        print_city_report(city, total_matched, ranked, args.top)

    if conn is not None:
        conn.close()
        print(
            f"\nUpdated {len(cities)} cit{'y' if len(cities) == 1 else 'ies'} "
            f"(source='google') in the combined SQLite dataset: {args.db_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
