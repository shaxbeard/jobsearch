#!/usr/bin/env python3
"""
Discover new company job boards on the supported ATS platforms and merge
them into ats_companies.json.

This is a maintenance/growth tool, meant to be run occasionally by whoever
maintains the app (e.g. weekly via cron) -- NOT per end-user search. The
actual job search in ats_job_search.py never calls Google/Serper; it only
ever hits the free ATS APIs directly. This script is the one place that
still uses Serper, and only to grow the company *list*, so its query volume
stays flat regardless of how many people use the app.

How it works:
  1. Runs one broad Google search per platform (via Serper) for that
     platform's public job-board URLs, e.g. site:boards.greenhouse.io.
  2. Extracts the company "slug" (board token) from each result URL.
  3. Skips slugs already in ats_companies.json.
  4. Validates each new slug against the ATS's own public API (confirms it
     resolves and has at least one open posting) before adding it.
  5. Merges validated companies into ats_companies.json.

Known limitation: this only finds companies that host jobs on the ATS's
*default* hosted domain (boards.greenhouse.io/{slug}, jobs.lever.co/{slug},
jobs.ashbyhq.com/{slug}). Companies that embed the same ATS under a custom
domain (e.g. a "careers" page on their own site) won't be discovered this
way even though their board token still works fine once known -- add those
manually to ats_companies.json.

Setup: same as google_job_search.py -- needs SERPER_API_KEY in .env.

Usage:
  python discover_ats_companies.py
  python discover_ats_companies.py --max-pages 5
  python discover_ats_companies.py --platforms greenhouse --dry-run
  python discover_ats_companies.py --cities "toronto,dallas,san francisco"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3
from dotenv import load_dotenv

from jobtrends.sources.ats_job_search import (
    DEFAULT_COMPANIES_FILE,
    fetch_ashby,
    fetch_greenhouse,
    fetch_lever,
    fetch_smartrecruiters,
    fetch_workday,
)

SERPER_URL = "https://google.serper.dev/search"
FREE_TIER_MAX_RESULTS = 10
DEFAULT_MAX_PAGES = 5

# One broad discovery query per platform. Deliberately NOT filtered by
# language/city/date -- the goal here is just to surface as many distinct
# company job-board URLs as possible, not to search job content.
DISCOVERY_QUERIES = {
    "greenhouse": "site:boards.greenhouse.io | site:job-boards.greenhouse.io (engineer | developer)",
    "lever": "site:jobs.lever.co (engineer | developer)",
    "ashby": "site:jobs.ashbyhq.com (engineer | developer)",
    "workday": "site:myworkdayjobs.com (engineer | developer)",
    "smartrecruiters": "site:jobs.smartrecruiters.com (engineer | developer)",
}


def build_discovery_query(platform: str, city: str | None) -> str:
    """Build the discovery query for a platform, optionally targeted at a
    city. Appending a quoted city phrase surfaces companies whose postings
    mention that city, which is how you grow coverage for a specific job
    market instead of just whatever generic companies Google happens to
    rank highest for the platform overall.
    """
    query = DISCOVERY_QUERIES[platform]
    if city:
        query = f'{query} "{city}"'
    return query

# Board-token (slug) is always the first path segment on each platform's
# default hosted job-board URL, e.g.:
#   https://boards.greenhouse.io/{slug}/jobs/12345
#   https://jobs.lever.co/{slug}/<uuid>
#   https://jobs.ashbyhq.com/{slug}/<uuid>
SLUG_PATTERN = re.compile(r"^/([a-z0-9][a-z0-9\-]*)/", re.IGNORECASE)

# Workday's slug is different: "{tenant}.wd{n}/{site}", taken from a job URL
# like https://{tenant}.wd{n}.myworkdayjobs.com/en-US/{site}/job/... -- the
# board token isn't a single path segment, so it needs its own extractor.
WORKDAY_HOST_PATTERN = re.compile(r"^([a-z0-9\-]+\.wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)
LOCALE_SEGMENT_PATTERN = re.compile(r"^[a-z]{2}-[A-Z]{2}$")

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
}


def search_page(query: str, api_key: str, *, page: int, num: int, verify: bool) -> dict:
    response = requests.post(
        SERPER_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "page": page, "num": num},
        timeout=30,
        verify=verify,
    )
    response.raise_for_status()
    return response.json()


def extract_slug(url: str, platform: str = "greenhouse") -> str | None:
    if platform == "workday":
        return extract_workday_slug(url)
    match = SLUG_PATTERN.match(urlparse(url).path)
    return match.group(1).lower() if match else None


def extract_workday_slug(url: str) -> str | None:
    parsed = urlparse(url)
    host_match = WORKDAY_HOST_PATTERN.match(parsed.netloc.lower())
    if not host_match:
        return None
    subdomain = host_match.group(1)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    # Path looks like /en-US/{site}/job/... or /{site}/job/... (no locale segment).
    site = parts[1] if LOCALE_SEGMENT_PATTERN.match(parts[0]) and len(parts) > 1 else parts[0]
    return f"{subdomain}/{site}"


def discover_slugs(query: str, api_key: str, *, max_pages: int, verify: bool, platform: str = "greenhouse") -> set[str]:
    slugs: set[str] = set()
    for page in range(1, max_pages + 1):
        data = search_page(query, api_key, page=page, num=FREE_TIER_MAX_RESULTS, verify=verify)
        organic = data.get("organic", [])
        if not organic:
            break
        for item in organic:
            slug = extract_slug(item.get("link", ""), platform)
            if slug:
                slugs.add(slug)
        if len(organic) < FREE_TIER_MAX_RESULTS:
            break
    return slugs


def guess_company_name(platform: str, slug: str, jobs: list[dict]) -> str:
    if platform == "greenhouse" and jobs:
        name = jobs[0].get("company_name")
        if name:
            return name.strip()
    if platform == "workday":
        tenant = slug.split(".")[0]
        return tenant.replace("-", " ").title()
    return slug.replace("-", " ").title()


def validate_slug(platform: str, slug: str, *, verify: bool) -> tuple[bool, str, int]:
    """Confirm `slug` is a real, active board on `platform`.

    Returns (is_valid, company_name, job_count).
    """
    try:
        jobs = FETCHERS[platform](slug, verify=verify)
    except requests.RequestException:
        return False, slug, 0
    if not jobs:
        return False, slug, 0
    return True, guess_company_name(platform, slug, jobs), len(jobs)


def load_companies_file(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"_comment": "Seed list of companies for ats_job_search.py.", "companies": []}


def save_companies_file(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover supported ATS company boards and merge them into ats_companies.json."
    )
    parser.add_argument(
        "--companies-file",
        type=Path,
        default=DEFAULT_COMPANIES_FILE,
        help=f"Company list JSON to update (default: {DEFAULT_COMPANIES_FILE.name})",
    )
    parser.add_argument(
        "--platforms",
        default=",".join(DISCOVERY_QUERIES),
        help=f"Comma-separated platforms to search (choices: {', '.join(DISCOVERY_QUERIES)})",
    )
    parser.add_argument(
        "--cities",
        default=None,
        help=(
            "Comma-separated cities to target discovery for, e.g. 'toronto,dallas,san francisco'. "
            "Each city is appended as a quoted phrase to the per-platform query, so results are "
            "companies with postings mentioning that city -- use this to grow coverage for a "
            "specific job market. If omitted, runs one generic (non-city) search per platform."
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Max Serper result pages to fetch per platform (default: {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and validate candidates but don't write ats_companies.json",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification (useful on some corporate networks)",
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

    platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    unknown = [p for p in platforms if p not in DISCOVERY_QUERIES]
    if unknown:
        valid = ", ".join(DISCOVERY_QUERIES)
        print(f"Error: unknown platform(s): {', '.join(unknown)}. Valid options: {valid}", file=sys.stderr)
        return 1

    verify = not args.insecure
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    companies_data = load_companies_file(args.companies_file)
    existing = {(c["platform"], c["slug"]) for c in companies_data["companies"]}

    cities: list[str | None] = (
        [c.strip() for c in args.cities.split(",") if c.strip()] if args.cities else [None]
    )

    added: list[tuple[str, str, str, int, str | None]] = []
    for platform in platforms:
        for city in cities:
            query = build_discovery_query(platform, city)
            tag = f"{platform}" + (f" | {city}" if city else "")
            print(f"[{tag}] searching: {query}")
            try:
                candidate_slugs = discover_slugs(
                    query, api_key, max_pages=args.max_pages, verify=verify, platform=platform
                )
            except requests.RequestException as exc:
                print(f"[{tag}] search failed: {exc}", file=sys.stderr)
                continue

            new_slugs = sorted(s for s in candidate_slugs if (platform, s) not in existing)
            print(f"[{tag}] found {len(candidate_slugs)} candidate slug(s), {len(new_slugs)} not already tracked")

            for slug in new_slugs:
                is_valid, name, job_count = validate_slug(platform, slug, verify=verify)
                if not is_valid:
                    continue
                companies_data["companies"].append({"name": name, "platform": platform, "slug": slug})
                existing.add((platform, slug))
                added.append((platform, slug, name, job_count, city))
                suffix = f" [{city}]" if city else ""
                print(f"  + {name} ({platform}/{slug}) -- {job_count} open posting(s){suffix}")

    if not added:
        print("\nNo new companies discovered.")
        return 0

    print(f"\n{len(added)} new compan{'y' if len(added) == 1 else 'ies'} discovered "
          f"(total now {len(companies_data['companies'])}).")

    if args.dry_run:
        print("Dry run: not saving ats_companies.json.")
    else:
        save_companies_file(args.companies_file, companies_data)
        print(f"Saved: {args.companies_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
