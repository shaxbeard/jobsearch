#!/usr/bin/env python3
"""
Search Google for job postings and save results to CSV + JSON.

Uses Serper (https://serper.dev) to run Google queries — the same approach
used elsewhere in this repo. Free tier: 2,500 queries, max 10 results per request.
Use --paid if you have a paid Serper plan (up to 100 results per request).

Setup:
  1. Sign up at https://serper.dev and copy your API key
  2. Add to your project .env file:
       SERPER_API_KEY=your_key_here
  3. Run:
       python zz_projects/google_job_search.py

Optional flags:
       python zz_projects/google_job_search.py --max-pages 5 --output-dir zz_projects/data
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv


# ATS/job-board domains to search via `site:`. Kept as a list (rather than one
# giant OR'd query) because Google/Serper tends to under-serve results once
# too many `site:` clauses are OR'd together in a single query — see
# build_site_queries(), which batches these into several smaller queries.
DEFAULT_ATS_SITES = [
    "lever.co",
    "greenhouse.io",
    "jobs.ashbyhq.com",
    "myworkdayjobs.com",
    # "smartrecruiters.com",
    # "icims.com",
    # "workable.com",
    # "bamboohr.com",
    # "breezy.hr",
    # "recruitee.com",
    # "personio.com",
    # "teamtailor.com",
    # "jobvite.com",
    # "taleo.net",
    # "successfactors.com",
    # "eightfold.ai",
]
DEFAULT_CITY = "toronto"
ROLE_FILTER_TEMPLATE = '(engineer | developer) ("{city}") -staff -lead -principal after:2026-01-01'
DEFAULT_SITE_BATCH_SIZE = 5
SERPER_URL = "https://google.serper.dev/search"
FREE_TIER_MAX_RESULTS = 10
PAID_TIER_MAX_RESULTS = 100

# Top 5 programming languages/groups most likely to be named in North American
# developer job ads in 2026 (per Stack Overflow/Indeed/LinkedIn job-posting
# trends): JavaScript (incl. TypeScript, since job ads overwhelmingly lump the
# two together), Python, Java, C#, and C++. Each maps to the quoted phrase(s)
# OR'd together to match it in a Google query.
LANGUAGE_KEYWORDS: dict[str, str] = {
    "javascript": '"javascript" | "typescript" | "react" | "node.js"',
    "python": '"python"',
    "java": '"java"',
    "csharp": '"c#"',
    "cpp": '"c++"',
}


def build_language_clause(languages: list[str]) -> str:
    """Build an OR'd, quoted-phrase clause requiring at least one language.

    Raises ValueError if a language isn't in LANGUAGE_KEYWORDS.
    """
    unknown = [lang for lang in languages if lang not in LANGUAGE_KEYWORDS]
    if unknown:
        valid = ", ".join(sorted(LANGUAGE_KEYWORDS))
        raise ValueError(f"Unknown language(s): {', '.join(unknown)}. Valid options: {valid}")
    return "(" + " | ".join(LANGUAGE_KEYWORDS[lang] for lang in languages) + ")"


def build_keyword_filter(city: str) -> str:
    """Build the role/city/date keyword filter for a given city."""
    return ROLE_FILTER_TEMPLATE.format(city=city)


def build_site_queries(
    sites: list[str],
    keyword_filter: str,
    *,
    batch_size: int = DEFAULT_SITE_BATCH_SIZE,
) -> list[str]:
    """Split `sites` into batches and build one `site:` OR query per batch.

    Google tends to ignore some `site:` clauses when too many are combined
    with `|` in one query, so we keep each query small and run several.
    """
    queries = []
    for i in range(0, len(sites), batch_size):
        batch = sites[i : i + batch_size]
        site_clause = " | ".join(f"site:{site}" for site in batch)
        queries.append(f"{site_clause} {keyword_filter}")
    return queries


def search_page(
    query: str,
    api_key: str,
    *,
    page: int = 1,
    num: int = 100,
    verify: bool = True,
) -> dict:
    response = requests.post(
        SERPER_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "page": page, "num": num},
        timeout=30,
        verify=verify,
    )
    response.raise_for_status()
    return response.json()


def collect_results(
    query: str,
    api_key: str,
    *,
    max_pages: int = 10,
    num_per_page: int = 100,
    verify: bool = True,
    seen_links: set[str] | None = None,
    results: list[dict] | None = None,
) -> list[dict]:
    """Collect results for one query, appending to `results` in place.

    Pass a shared `seen_links`/`results` across multiple calls to merge and
    dedupe results from several queries (e.g. one per site batch).
    """
    if seen_links is None:
        seen_links = set()
    if results is None:
        results = []

    for page in range(1, max_pages + 1):
        data = search_page(
            query,
            api_key,
            page=page,
            num=num_per_page,
            verify=verify,
        )
        organic = data.get("organic", [])
        if not organic:
            break

        for rank, item in enumerate(organic, start=1):
            link = item.get("link", "")
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            results.append(
                {
                    "rank": len(results) + 1,
                    "title": item.get("title", ""),
                    "link": link,
                    "domain": urlparse(link).netloc,
                    "snippet": item.get("snippet", ""),
                    "date": item.get("date", ""),
                    "page": page,
                    "position_on_page": rank,
                }
            )

        if len(organic) < num_per_page:
            break

    return results


def save_results(
    results: list[dict],
    query: str,
    output_dir: Path,
    *,
    label: str | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"google_search_{timestamp}"
    if label:
        base_name = f"{base_name}_{label}_{len(results)}results"

    json_path = output_dir / f"{base_name}.json"
    csv_path = output_dir / f"{base_name}.csv"

    payload = {
        "query": query,
        "searched_at_utc": datetime.now(timezone.utc).isoformat(),
        "result_count": len(results),
        "results": results,
    }

    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = ["rank", "title", "link", "domain", "snippet", "date", "page", "position_on_page"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    return json_path, csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search Google via Serper and save job results.")
    parser.add_argument(
        "--query",
        default=None,
        help=(
            "Full custom Google search query. If set, this is run as a single "
            "query and --sites/--keyword-filter/--site-batch-size are ignored."
        ),
    )
    parser.add_argument(
        "--sites",
        default=",".join(DEFAULT_ATS_SITES),
        help="Comma-separated list of ATS/job-board domains to search (used when --query is not set).",
    )
    parser.add_argument(
        "--city",
        default=DEFAULT_CITY,
        help=(
            f"City to filter job postings by (default: {DEFAULT_CITY}). "
            "Ignored when --keyword-filter is set."
        ),
    )
    parser.add_argument(
        "--keyword-filter",
        default=None,
        help=(
            "Full keyword/date filter appended to each site batch query, "
            "overriding --city entirely (e.g. to change roles, dates, or drop "
            "the city filter). Default is built from --city."
        ),
    )
    parser.add_argument(
        "--languages",
        default=",".join(LANGUAGE_KEYWORDS),
        help=(
            "Comma-separated programming language(s) to search for, one file "
            f"per language (choices: {', '.join(sorted(LANGUAGE_KEYWORDS))}). "
            "Defaults to all 5. Pass 'none' to disable language filtering "
            "and produce a single combined file instead. Ignored when --query is set."
        ),
    )
    parser.add_argument(
        "--site-batch-size",
        type=int,
        default=DEFAULT_SITE_BATCH_SIZE,
        help=(
            f"Number of `site:` domains to OR together per query (default: "
            f"{DEFAULT_SITE_BATCH_SIZE}). Google under-serves results if too many "
            "site: clauses are combined, so smaller batches (run as separate "
            "queries) give better coverage."
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum number of result pages to fetch (default: 10)",
    )
    parser.add_argument(
        "--num-per-page",
        type=int,
        default=FREE_TIER_MAX_RESULTS,
        help=f"Results per page (default: {FREE_TIER_MAX_RESULTS} for free Serper accounts)",
    )
    parser.add_argument(
        "--paid",
        action="store_true",
        help="Use paid Serper limits (up to 100 results per request)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="Directory for output files (default: zz_projects/data)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification (useful on some corporate networks)",
    )
    return parser.parse_args()


def run_queries(
    queries: list[str],
    api_key: str,
    *,
    max_pages: int,
    num_per_page: int,
    verify: bool,
) -> tuple[list[dict] | None, int | None]:
    """Run all `queries`, merging/deduping into one result list.

    Returns (results, None) on success, or (None, exit_code) on failure
    (after printing an error message).
    """
    seen_links: set[str] = set()
    results: list[dict] = []
    try:
        for query in queries:
            collect_results(
                query,
                api_key,
                max_pages=max_pages,
                num_per_page=num_per_page,
                verify=verify,
                seen_links=seen_links,
                results=results,
            )
    except requests.HTTPError as exc:
        body = exc.response.text
        print(f"Serper API error: {exc.response.status_code} {body}", file=sys.stderr)
        if (
            exc.response.status_code == 400
            and "Query pattern not allowed for free accounts" in body
            and num_per_page > FREE_TIER_MAX_RESULTS
        ):
            print(
                f"Tip: this usually means your Serper free account cannot request "
                f"more than {FREE_TIER_MAX_RESULTS} results at once. "
                f"Retry with --num-per-page {FREE_TIER_MAX_RESULTS} (the default).",
                file=sys.stderr,
            )
        return None, 1
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            print("Tip: retry with --insecure if you're on a corporate VPN/proxy.", file=sys.stderr)
        return None, 1

    return results, None


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

    verify = not args.insecure
    max_results = PAID_TIER_MAX_RESULTS if args.paid else FREE_TIER_MAX_RESULTS
    num_per_page = max(1, min(args.num_per_page, max_results))
    if not args.paid and args.num_per_page > FREE_TIER_MAX_RESULTS:
        print(
            f"Note: free Serper accounts are limited to {FREE_TIER_MAX_RESULTS} results "
            f"per request; using {num_per_page}.",
        )

    base_keyword_filter = (
        args.keyword_filter if args.keyword_filter is not None else build_keyword_filter(args.city)
    )

    if args.query:
        queries = [args.query]
        print(f"Running {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}:")
        for q in queries:
            print(f"  - {q}")
        print(f"Fetching up to {args.max_pages} page(s) with {num_per_page} results each...")

        results, err = run_queries(
            queries,
            api_key,
            max_pages=args.max_pages,
            num_per_page=num_per_page,
            verify=verify,
        )
        if err is not None:
            return err

        combined_query = " || ".join(queries)
        json_path, csv_path = save_results(results, combined_query, args.output_dir)

        print(f"Saved {len(results)} unique result(s).")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")
        return 0

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]

    if not args.languages or args.languages.strip().lower() == "none":
        # No language filter: one combined search/file, as before.
        queries = build_site_queries(sites, base_keyword_filter, batch_size=args.site_batch_size)

        print(f"Running {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}:")
        for q in queries:
            print(f"  - {q}")
        print(f"Fetching up to {args.max_pages} page(s) with {num_per_page} results each...")

        results, err = run_queries(
            queries,
            api_key,
            max_pages=args.max_pages,
            num_per_page=num_per_page,
            verify=verify,
        )
        if err is not None:
            return err

        combined_query = " || ".join(queries)
        json_path, csv_path = save_results(results, combined_query, args.output_dir, label=args.city)

        print(f"Saved {len(results)} unique result(s).")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")
        return 0

    # One or more languages requested: run each language as its own search
    # and save each to its own file (filename includes language + count).
    languages = [lang.strip().lower() for lang in args.languages.split(",") if lang.strip()]
    try:
        for lang in languages:
            build_language_clause([lang])  # validate all languages up front
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_results = 0
    for lang in languages:
        keyword_filter = f"{build_language_clause([lang])} {base_keyword_filter}"
        queries = build_site_queries(sites, keyword_filter, batch_size=args.site_batch_size)

        print(f"\n[{lang}] Running {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}:")
        for q in queries:
            print(f"  - {q}")
        print(f"[{lang}] Fetching up to {args.max_pages} page(s) with {num_per_page} results each...")

        results, err = run_queries(
            queries,
            api_key,
            max_pages=args.max_pages,
            num_per_page=num_per_page,
            verify=verify,
        )
        if err is not None:
            return err

        combined_query = " || ".join(queries)
        json_path, csv_path = save_results(
            results, combined_query, args.output_dir, label=f"{args.city}_{lang}"
        )

        total_results += len(results)
        print(f"[{lang}] Saved {len(results)} unique result(s).")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")

    print(f"\nDone. {total_results} total result(s) across {len(languages)} language file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
