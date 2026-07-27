#!/usr/bin/env python3
"""
Search developer job postings by crawling ATS platforms' free public
job-board APIs directly (Greenhouse, Lever, Ashby) -- no Google, no Serper,
no API key, and no per-query rate limit. This makes it suitable for a
multi-user app: your own crawl volume stays roughly constant no matter how
many end users query the results, because the crawl runs once and the
results are filtered/served from what was fetched.

This is a companion to google_job_search.py and does not modify or depend
on it -- everything needed lives in this file (plus ats_companies.json).

How it works:
  1. Reads a list of companies (name + ATS platform + board slug) from
     ats_companies.json.
  2. Fetches each company's public job postings directly from Greenhouse,
     Lever, or Ashby's own JSON APIs (concurrently, since these are just
     independent HTTP GETs).
  3. Normalizes postings from all three platforms into one common schema.
  4. Filters by city, "posted since" date, and programming language
     (title/description keyword match).
  5. Saves one JSON+CSV file per language (same spirit as
     google_job_search.py), named:
       ats_search_{timestamp}_{city}_{language}_{count}results.{json,csv}

Usage:
  python ats_job_search.py
  python ats_job_search.py --city dallas --languages python,cpp
  python ats_job_search.py --languages none          # one combined file
  python ats_job_search.py --insecure                # corporate VPN/proxy SSL issues

Adding companies:
  Edit ats_companies.json and add an entry:
    {"name": "Acme", "platform": "greenhouse", "slug": "acme"}
  The slug is the token in that company's public board URL, e.g.:
    https://boards.greenhouse.io/{slug}
    https://jobs.lever.co/{slug}
    https://jobs.ashbyhq.com/{slug}
  Workday is different -- its slug is "{tenant}.wd{n}/{site}" (the career-site
  subdomain plus the site path segment), taken from a job URL like:
    https://{tenant}.wd{n}.myworkdayjobs.com/en-US/{site}/job/...
  e.g. "texascapitalbank.wd12/Careers".
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
WORKDAY_DETAIL_MAX_WORKERS = 10
# Safety caps so a mega-employer with tens of thousands of postings (e.g. a
# large retailer/bank on Workday) can't turn a crawl into a runaway multi-
# thousand-request scan -- coverage becomes a best-effort sample past these
# caps rather than exhaustive, which is an acceptable tradeoff since the vast
# majority of such employers' postings aren't software engineering roles.
WORKDAY_MAX_LIST_ITEMS = 3000
WORKDAY_MAX_DETAIL_FETCHES = 500

DEFAULT_COMPANIES_FILE = Path(__file__).resolve().parent / "ats_companies.json"
DEFAULT_CITY = "toronto"
DEFAULT_SINCE_DATE = "2026-01-01"
DEFAULT_MAX_WORKERS = 8

# By default, only keep postings that look like individual-contributor
# software engineer/developer roles -- title must contain one of these
# (covers common synonym titles for the same coding IC role across
# companies, not just the literal words "software engineer")...
DEFAULT_TITLE_INCLUDE = (
    "software engineer,software developer,software development engineer,"
    "backend engineer,back-end engineer,frontend engineer,front-end engineer,"
    "full stack engineer,full-stack engineer,full stack developer,full-stack developer,"
    "platform engineer,application developer,web developer,mobile engineer,mobile developer"
)
# ...and must NOT contain any of these (drops management/leadership titles
# like "Engineering Manager", "Director of Engineering", etc., even if the
# title also happens to contain "software engineer"/"engineering").
DEFAULT_TITLE_EXCLUDE = "manager,director,vp ,vice president,head of,chief"

# Top 5 programming languages/groups most likely to be named in North American
# developer job ads in 2026: JavaScript (incl. TypeScript), Python, Java, C#,
# C++. Each maps to the plain lowercase phrase(s) checked for in job text.
LANGUAGE_PHRASES: dict[str, list[str]] = {
    "javascript": ["javascript", "typescript"],
    "python": ["python"],
    "java": ["java"],
    "csharp": ["c#"],
    "cpp": ["c++"],
}

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = HTML_TAG_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def load_companies(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["companies"]


def fetch_json(url: str, *, verify: bool, timeout: int = 20):
    response = requests.get(url, timeout=timeout, verify=verify)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def fetch_greenhouse(slug: str, *, verify: bool) -> list[dict]:
    data = fetch_json(GREENHOUSE_URL.format(slug=slug), verify=verify)
    if not data:
        return []
    return data.get("jobs", [])


def fetch_lever(slug: str, *, verify: bool) -> list[dict]:
    data = fetch_json(LEVER_URL.format(slug=slug), verify=verify)
    return data or []


def fetch_ashby(slug: str, *, verify: bool) -> list[dict]:
    data = fetch_json(ASHBY_URL.format(slug=slug), verify=verify)
    if not data:
        return []
    return data.get("jobs", [])


def parse_workday_slug(slug: str) -> tuple[str, str, str]:
    """Parse a Workday slug '{tenant}.wd{n}/{site}' into (subdomain, tenant, site),
    e.g. 'texascapitalbank.wd12/Careers' -> ('texascapitalbank.wd12', 'texascapitalbank', 'Careers').
    """
    subdomain, _, site = slug.partition("/")
    tenant = subdomain.split(".")[0]
    return subdomain, tenant, site


def fetch_workday(slug: str, *, verify: bool) -> list[dict]:
    """Fetch job postings for a Workday-hosted company career site, using the
    same public JSON API Workday's own career-site widget uses (no API key
    or auth required).

    Unlike Greenhouse/Lever/Ashby, Workday's list endpoint only returns a
    summary per posting (title/location/path) -- the full description needs
    a second request per posting. Large enterprises can list thousands of
    postings across every department (retail, finance, HR, etc.), so before
    paying for a detail request per posting, summaries are pre-filtered by
    title against the default SWE-role keywords (DEFAULT_TITLE_INCLUDE/
    DEFAULT_TITLE_EXCLUDE) -- non-matching postings would just get dropped
    by matches_role() downstream anyway. Note: this means a custom, broader
    --title-include passed to main() won't rescue postings this pre-filter
    already excluded; only the defaults are consulted here.
    """
    subdomain, tenant, site = parse_workday_slug(slug)
    base = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"

    summaries: list[dict] = []
    offset = 0
    page_size = 20
    while True:
        response = requests.post(
            f"{base}/jobs",
            json={"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": ""},
            timeout=20,
            verify=verify,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        data = response.json()
        postings = data.get("jobPostings", [])
        summaries.extend(postings)
        offset += page_size
        total = data.get("total", 0)
        if not postings or offset >= total:
            break
        if len(summaries) >= WORKDAY_MAX_LIST_ITEMS:
            print(
                f"  [workday] {tenant}: capped listing at {WORKDAY_MAX_LIST_ITEMS} of "
                f"{total} total postings (mega-employer safety cap)",
                file=sys.stderr,
            )
            break

    include_keywords = parse_keyword_list(DEFAULT_TITLE_INCLUDE)
    exclude_keywords = parse_keyword_list(DEFAULT_TITLE_EXCLUDE)
    candidates = [
        summary
        for summary in summaries
        if matches_role(
            {"title": summary.get("title", "")},
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
        )
    ]
    if len(candidates) > WORKDAY_MAX_DETAIL_FETCHES:
        print(
            f"  [workday] {tenant}: capped detail fetches at {WORKDAY_MAX_DETAIL_FETCHES} of "
            f"{len(candidates)} title-matched postings (mega-employer safety cap)",
            file=sys.stderr,
        )
        candidates = candidates[:WORKDAY_MAX_DETAIL_FETCHES]

    def fetch_detail(summary: dict) -> dict | None:
        external_path = summary.get("externalPath")
        if not external_path:
            return None
        detail: dict = {}
        try:
            detail_response = requests.get(f"{base}{external_path}", timeout=20, verify=verify)
            detail_response.raise_for_status()
            detail = detail_response.json().get("jobPostingInfo", {})
        except requests.RequestException:
            pass
        return {
            "title": detail.get("title") or summary.get("title", ""),
            "location": detail.get("location") or summary.get("locationsText", ""),
            "url": detail.get("externalUrl") or f"https://{subdomain}.myworkdayjobs.com{external_path}",
            "posted_at": detail.get("startDate", ""),
            "description": strip_html(detail.get("jobDescription")),
        }

    jobs: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKDAY_DETAIL_MAX_WORKERS) as executor:
        for result in executor.map(fetch_detail, candidates):
            if result:
                jobs.append(result)
    return jobs


def normalize_greenhouse(job: dict, company_name: str) -> dict:
    return {
        "company": company_name,
        "platform": "greenhouse",
        "title": (job.get("title") or "").strip(),
        "location": (job.get("location") or {}).get("name", "") or "",
        "url": job.get("absolute_url", ""),
        "posted_at": job.get("first_published") or job.get("updated_at") or "",
        "description": strip_html(job.get("content")),
    }


def normalize_lever(job: dict, company_name: str) -> dict:
    categories = job.get("categories") or {}
    created_at_ms = job.get("createdAt")
    posted_at = ""
    if isinstance(created_at_ms, (int, float)):
        posted_at = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc).isoformat()
    return {
        "company": company_name,
        "platform": "lever",
        "title": (job.get("text") or "").strip(),
        "location": categories.get("location", "") or "",
        "url": job.get("hostedUrl", ""),
        "posted_at": posted_at,
        "description": strip_html(job.get("descriptionPlain") or job.get("description")),
    }


def normalize_ashby(job: dict, company_name: str) -> dict:
    locations = [job.get("location", "")]
    for extra in job.get("secondaryLocations", []) or []:
        loc = extra.get("location")
        if loc:
            locations.append(loc)
    return {
        "company": company_name,
        "platform": "ashby",
        "title": (job.get("title") or "").strip(),
        "location": " | ".join(loc for loc in locations if loc),
        "url": job.get("jobUrl", ""),
        "posted_at": job.get("publishedAt", ""),
        "description": strip_html(job.get("descriptionHtml")),
    }


def normalize_workday(job: dict, company_name: str) -> dict:
    return {
        "company": company_name,
        "platform": "workday",
        "title": (job.get("title") or "").strip(),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "posted_at": job.get("posted_at", ""),
        "description": job.get("description", ""),
    }


FETCHERS = {
    "greenhouse": (fetch_greenhouse, normalize_greenhouse),
    "lever": (fetch_lever, normalize_lever),
    "ashby": (fetch_ashby, normalize_ashby),
    "workday": (fetch_workday, normalize_workday),
}


def collect_company_postings(company: dict, *, verify: bool) -> list[dict]:
    platform = company["platform"]
    slug = company["slug"]
    name = company.get("name", slug)

    if platform not in FETCHERS:
        raise ValueError(f"unknown platform {platform!r}")

    fetch_fn, normalize_fn = FETCHERS[platform]
    raw_jobs = fetch_fn(slug, verify=verify)
    return [normalize_fn(job, name) for job in raw_jobs]


def collect_all_postings(
    companies: list[dict],
    *,
    verify: bool,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> tuple[list[dict], list[str]]:
    """Fetch postings for all companies concurrently.

    Returns (postings, errors). A failed company is skipped (not fatal) and
    its error message is added to `errors`.
    """
    postings: list[dict] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_company = {
            executor.submit(collect_company_postings, company, verify=verify): company
            for company in companies
        }
        for future in as_completed(future_to_company):
            company = future_to_company[future]
            try:
                postings.extend(future.result())
            except Exception as exc:  # one bad company shouldn't kill the whole run
                errors.append(f"{company.get('name', company.get('slug'))}: {exc}")

    return postings, errors


def parse_job_url(url: str) -> tuple[str, str, str] | None:
    """Map a public job-posting URL to (platform, slug, job_key).

    Recognizes the default hosted board URLs for the three API-crawlable
    platforms and returns a stable key that's identical whether the URL came
    from a Google search result or from a normalized posting fetched via the
    ATS API, so the two can be matched up:

      https://boards.greenhouse.io/{slug}/jobs/{id}      -> ("greenhouse", slug, id)
      https://job-boards.greenhouse.io/{slug}/jobs/{id}  -> ("greenhouse", slug, id)
      https://jobs.lever.co/{slug}/{uuid}[/apply]        -> ("lever", slug, uuid)
      https://jobs.ashbyhq.com/{slug}/{uuid}             -> ("ashby", slug, uuid)

    Query strings and a trailing "/apply" segment are ignored. Returns None
    for anything that isn't one of these board URL shapes.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[-1].lower() == "apply":
        parts = parts[:-1]
    if not parts:
        return None
    if "greenhouse.io" in host:
        if len(parts) >= 3 and parts[1].lower() == "jobs":
            return "greenhouse", parts[0].lower(), parts[2]
        return None
    if host == "jobs.lever.co":
        if len(parts) >= 2:
            return "lever", parts[0].lower(), parts[1]
        return None
    if host == "jobs.ashbyhq.com":
        if len(parts) >= 2:
            return "ashby", parts[0].lower(), parts[1]
        return None
    return None


def fetch_postings_for_urls(
    urls: list[str],
    *,
    verify: bool,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> tuple[list[dict], list[str], list[str]]:
    """Fetch full normalized postings for a set of job-posting URLs.

    Given URLs (e.g. from a Google search restricted to the three supported
    platforms), this groups them by (platform, slug), fetches each company's
    board once via the same public API used everywhere else, and returns only
    the specific postings whose URLs were asked for -- with full descriptions,
    so language detection has real text to work with instead of a Google
    snippet.

    Returns (matched_postings, unresolved_urls, errors):
      - matched_postings: normalized posting dicts (deduped) that were found.
      - unresolved_urls:  requested URLs that couldn't be parsed as a supported
                          board URL, or whose posting wasn't found on the board
                          (e.g. filled/closed since Google indexed it).
      - errors:           "platform/slug: message" for boards that failed to fetch.
    """
    requested: dict[tuple[str, str, str], str] = {}
    groups: dict[tuple[str, str], set[str]] = {}
    unresolved: list[str] = []
    for url in urls:
        parsed = parse_job_url(url)
        if not parsed:
            unresolved.append(url)
            continue
        platform, slug, job_key = parsed
        requested[(platform, slug, job_key)] = url
        groups.setdefault((platform, slug), set()).add(job_key)

    matched: list[dict] = []
    errors: list[str] = []
    found_keys: set[tuple[str, str, str]] = set()

    def fetch_board(group: tuple[str, str]) -> list[dict]:
        platform, slug = group
        return collect_company_postings({"name": slug, "platform": platform, "slug": slug}, verify=verify)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_group = {executor.submit(fetch_board, group): group for group in groups}
        for future in as_completed(future_to_group):
            platform, slug = future_to_group[future]
            try:
                postings = future.result()
            except Exception as exc:  # one bad board shouldn't kill the whole run
                errors.append(f"{platform}/{slug}: {exc}")
                continue
            wanted = groups[(platform, slug)]
            for posting in postings:
                key = parse_job_url(posting.get("url", ""))
                if key and key[2] in wanted and key not in found_keys:
                    matched.append(posting)
                    found_keys.add(key)

    for key, url in requested.items():
        if key not in found_keys:
            unresolved.append(url)

    return matched, unresolved, errors


def _normalize_place(text: str) -> str:
    """Lowercase and normalize punctuation so multi-word place names match
    regardless of separators, e.g. 'Washington, DC' and 'Washington, D.C.'
    both become 'washington dc'.
    """
    lowered = text.lower().replace(".", "")  # D.C. -> dc, keeps abbreviations intact
    lowered = re.sub(r"[,/|]", " ", lowered)  # separators become spaces
    return re.sub(r"\s+", " ", lowered).strip()


def matches_city(posting: dict, city: str) -> bool:
    city_lower = _normalize_place(city)
    if not city_lower:
        return True
    haystack = _normalize_place(f"{posting['location']} {posting['description']}")
    return city_lower in haystack


def matches_language(posting: dict, phrases: list[str]) -> bool:
    haystack = f"{posting['title']} {posting['description']}".lower()
    for phrase in phrases:
        if phrase in ("c#", "c++"):
            if phrase in haystack:
                return True
        elif re.search(rf"\b{re.escape(phrase)}\b", haystack):
            return True
    return False


def matches_since_date(posting: dict, since_date: datetime | None) -> bool:
    if since_date is None:
        return True
    posted_at = posting.get("posted_at") or ""
    if not posted_at:
        return True  # unknown date: don't drop it
    try:
        posted_dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if posted_dt.tzinfo is None:
        posted_dt = posted_dt.replace(tzinfo=timezone.utc)
    return posted_dt >= since_date


def parse_keyword_list(raw: str) -> list[str]:
    """Parse a comma-separated CLI string into a lowercase keyword list.
    Pass '' or 'none' to get an empty (i.e. disabled) list back.
    """
    if not raw or raw.strip().lower() == "none":
        return []
    return [kw.strip().lower() for kw in raw.split(",") if kw.strip()]


def matches_role(posting: dict, *, include_keywords: list[str], exclude_keywords: list[str]) -> bool:
    """Keep a posting only if its title contains one of include_keywords (when
    non-empty) and none of exclude_keywords. Both are matched as lowercase
    substrings, e.g. exclude_keywords=['manager'] drops "Engineering Manager"
    and "Software Engineering Manager" alike, even though the latter also
    contains "software engineer" as a substring.
    """
    title = (posting.get("title") or "").lower()
    if include_keywords and not any(keyword in title for keyword in include_keywords):
        return False
    if exclude_keywords and any(keyword in title for keyword in exclude_keywords):
        return False
    return True


def save_results(
    postings: list[dict],
    output_dir: Path,
    *,
    label: str | None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"ats_search_{timestamp}"
    if label:
        base_name = f"{base_name}_{label}_{len(postings)}results"

    json_path = output_dir / f"{base_name}.json"
    csv_path = output_dir / f"{base_name}.csv"

    ranked = [{"rank": i + 1, **posting} for i, posting in enumerate(postings)]

    payload = {
        "searched_at_utc": datetime.now(timezone.utc).isoformat(),
        "result_count": len(ranked),
        "results": ranked,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = ["rank", "company", "platform", "title", "location", "url", "posted_at"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ranked)

    return json_path, csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Search developer job postings by crawling ATS platforms' free "
            "public job-board APIs directly (no Google/Serper, no API key, "
            "no query quota)."
        )
    )
    parser.add_argument(
        "--companies-file",
        type=Path,
        default=DEFAULT_COMPANIES_FILE,
        help=f"JSON file listing companies to crawl (default: {DEFAULT_COMPANIES_FILE.name})",
    )
    parser.add_argument(
        "--city",
        default=DEFAULT_CITY,
        help=f"City to filter job postings by (default: {DEFAULT_CITY}). Pass '' to disable.",
    )
    parser.add_argument(
        "--languages",
        default=",".join(LANGUAGE_PHRASES),
        help=(
            "Comma-separated programming language(s) to search for, one file "
            f"per language (choices: {', '.join(sorted(LANGUAGE_PHRASES))}). "
            "Defaults to all 5. Pass 'none' to disable language filtering and "
            "produce a single combined file instead."
        ),
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.companies_file.exists():
        print(f"Error: companies file not found: {args.companies_file}", file=sys.stderr)
        return 1

    companies = load_companies(args.companies_file)
    verify = not args.insecure
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    since_date = None
    if args.since_date and args.since_date.strip().lower() != "none":
        try:
            since_date = datetime.fromisoformat(args.since_date).replace(tzinfo=timezone.utc)
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

    filtered = [
        p
        for p in all_postings
        if matches_city(p, args.city)
        and matches_since_date(p, since_date)
        and matches_role(p, include_keywords=title_include, exclude_keywords=title_exclude)
    ]
    print(
        f"{len(filtered)} posting(s) match city={args.city!r}, since={args.since_date}, "
        f"title-include={args.title_include!r}, title-exclude={args.title_exclude!r}."
    )

    if not args.languages or args.languages.strip().lower() == "none":
        json_path, csv_path = save_results(filtered, args.output_dir, label=args.city or None)
        print(f"Saved {len(filtered)} result(s).")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")
        return 0

    languages = [lang.strip().lower() for lang in args.languages.split(",") if lang.strip()]
    unknown = [lang for lang in languages if lang not in LANGUAGE_PHRASES]
    if unknown:
        valid = ", ".join(sorted(LANGUAGE_PHRASES))
        print(f"Error: unknown language(s): {', '.join(unknown)}. Valid options: {valid}", file=sys.stderr)
        return 1

    total = 0
    for lang in languages:
        lang_postings = [p for p in filtered if matches_language(p, LANGUAGE_PHRASES[lang])]
        label = f"{args.city}_{lang}" if args.city else lang
        json_path, csv_path = save_results(lang_postings, args.output_dir, label=label)
        total += len(lang_postings)
        print(f"[{lang}] Saved {len(lang_postings)} result(s).")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")

    print(f"\nDone. {total} total result(s) across {len(languages)} language file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
