# Hit List Magic Tracker

## How to run
Start the Flask development server:

```bash
jobtrends-web
```

Manually update the data

```bash
jobtrends-google-trends --insecure
```

Hit List Magic Tracker follows software-engineering hiring activity across North American cities. Programming-language demand, estimated from the full text of each job description, is one of the metrics it tracks. The app combines Google-based discovery, public applicant-tracking-system (ATS) endpoints, an incremental SQLite dataset, and a Flask frontend.

The project is intended to answer questions such as:

- How many software-engineering jobs are appearing in each city?
- Which programming languages are mentioned most often overall and by city?
- Which job titles are most common?
- Which individual postings contribute to each city's statistics?

This is a market-signal tool, not a complete census of every available job. Google indexing, result ranking, ATS behavior, and the project's filters all affect coverage.


## Installation

The project is packaged as an installable Python package (`jobtrends`) with a
`src/` layout. Install it once in editable mode; this registers the
command-line entry points (`jobtrends-*`) used throughout this document:

```bash
python -m pip install -e .
```

## Project Layout

```text
src/jobtrends/
  language_detect.py          shared language detection
  trends_db.py                SQLite persistence
  paths.py                    runtime path resolution (DB, data/, config/)
  sources/                    ats_job_search, google_job_search, discover_ats_companies
  analysis/                   language_trends, google_language_trends, trends_stats
  web/                        Flask app.py + templates/ + static/
config/ats_companies.json     employer boards for the fixed-company crawl
scripts/                      run_daily_update.sh, install_daily_cron.sh
tests/                        unit tests
job_trends.db                 local SQLite dataset (repository root)
data/                         local cron log and ad-hoc search-CLI exports
```

## Quick Version of Running the Data Pipeline

Update all tracked cities:

```bash
jobtrends-google-trends --insecure
```

## Architecture

```mermaid
flowchart LR
    A[Serper / Google search] -->|candidate job URLs| B[URL parser]
    B --> C[Greenhouse adapter]
    B --> D[Lever adapter]
    B --> E[Ashby adapter]
    B --> F[Workday adapter]
    C --> G[Normalized full postings]
    D --> G
    E --> G
    F --> G
    G --> H[Role, date, and city filters]
    H --> I[Language detection]
    I --> J[(SQLite)]
    J --> K[Stats snapshots]
    J --> L[Flask JSON API]
    L --> M[Leaflet frontend]
```

The normal workflow is:

1. `google_language_trends.py` builds one Google query per city, restricted to the five supported ATS domains.
2. Serper returns indexed job-posting URLs. Serper is a paid/free-quota Google Search API; it is the only component that consumes search credits.
3. `ats_job_search.py` parses each URL into a platform, employer board, and stable job key.
4. The corresponding public ATS endpoint returns the complete job description. These requests do not consume Serper credits.
5. The posting is normalized and filtered by role, date, and city.
6. `language_detect.py` detects language mentions in the title and full description.
7. New postings are appended to `job_trends.db`; aggregate city/language counts are recalculated from the accumulated postings.
8. `app.py` reads the database and serves the map, statistics, and posting drill-down UI.

## Why These Five Domains?

The project searches these hosted job-board domains:

```text
lever.co
greenhouse.io
jobs.ashbyhq.com
myworkdayjobs.com
jobs.smartrecruiters.com
```

These are applicant tracking systems rather than general-purpose aggregators such as Indeed or Dice. Employers publish jobs directly through them. They were selected for several reasons:

- **Full descriptions are publicly retrievable.** Their career pages use public structured endpoints that provide the complete posting without employer API credentials.
- **URLs contain stable identifiers.** The platform, employer board, and job ID can be extracted and used for incremental deduplication.
- **They complement one another.** Greenhouse, Lever, and Ashby are common among technology companies and startups. Workday and SmartRecruiters add coverage for larger and more varied employers that are underrepresented on those platforms.
- **They preserve provenance.** Results point to an employer's ATS-hosted posting rather than a copied aggregator listing.
- **They avoid brittle page scraping.** Structured JSON is generally more reliable than extracting descriptions from arbitrary HTML.

All five domains are combined into one Google query per city to conserve Serper quota. This is cheaper than giving each platform its own query, but Google's limited result slots may favor one platform over another. A platform-specific audit can be run with `--sites`, for example:

```bash
jobtrends-google-trends --sites myworkdayjobs.com --insecure
```

Workday and SmartRecruiters Google-discovered URLs are resolved directly through their public posting-detail endpoints. City validation uses each ATS's structured location field, avoiding jobs accepted merely because another city appears in the description.

Dallas and Houston are treated as metro areas without adding Serper queries. Dallas includes Fort Worth, Plano, Irving, and Richardson; Houston includes The Woodlands and Sugar Land. Postings are stored under the primary tracked city for aggregate reporting.

## Key Files

| Module | Command | Responsibility |
|---|---|---|
| `src/jobtrends/analysis/google_language_trends.py` | `jobtrends-google-trends` | Main incremental pipeline: discovery, filtering, persistence, reports, and exports. |
| `src/jobtrends/sources/google_job_search.py` | — | Serper client and construction/batching of Google `site:` queries. |
| `src/jobtrends/sources/ats_job_search.py` | `jobtrends-ats` | ATS adapters, URL parsing, normalization, role/date/city filters, and full-description retrieval. |
| `src/jobtrends/language_detect.py` | — | Shared language vocabulary, detection, counting, and ranking logic. |
| `src/jobtrends/trends_db.py` | — | SQLite schema and persistence helpers. |
| `src/jobtrends/analysis/trends_stats.py` | `jobtrends-stats` | Overall/per-city statistics and stable text/JSON snapshots. |
| `src/jobtrends/web/app.py` | `jobtrends-web` | Flask application and JSON API. |
| `src/jobtrends/web/templates/index.html` | — | Frontend document structure. |
| `src/jobtrends/web/static/js/app.js` | — | Leaflet map, city labels, API calls, charts, and modal behavior. |
| `src/jobtrends/web/static/css/style.css` | — | Responsive light-mode presentation. |
| `src/jobtrends/analysis/language_trends.py` | `jobtrends-trends` | Optional comparison pipeline that crawls a fixed employer list without Google. |
| `config/ats_companies.json` | — | Employer boards used by the fixed-company crawl. |
| `src/jobtrends/sources/discover_ats_companies.py` | `jobtrends-discover` | Occasional Serper-assisted discovery of employer boards. |

## Data Model

The default database is `job_trends.db` in the project root. Set `JOB_TRENDS_DB` to use another path, such as a mounted production disk.

The main tables are:

- `postings`: normalized job data, full descriptions, detected languages, platform, city, source, and timestamps.
- `cities`: one summary row per `(city, source)`, including total postings and the latest fetch watermark.
- `city_language_counts`: ranked language counts and percentages per `(city, source)`.

Data is separated by source:

- `google`: postings found through the normal Google/Serper pipeline. The frontend currently reads this source.
- `ats`: postings found by the optional fixed-company crawl.

The Google pipeline is append-only for accepted postings. It keeps accumulated job history rather than deleting older or subsequently closed jobs. Statistics, map totals, rankings, and frontend posting lists use only postings whose advertised date falls within the rolling past six calendar months. Older postings remain in `postings` for history and URL deduplication but do not contribute to current statistics. The project does not currently track a separate run-history table or actively retire closed postings.

## Incremental Updates

Each city is updated independently:

- The first fetch uses `--since-date` as its lower bound (default: `2026-01-01`).
- Later fetches resume from that city's last update with a three-day lookback.
- The overlap protects against jobs that Google indexes after their advertised posting date.
- Stable platform/job keys prevent duplicate storage during the overlap.
- Language counts are recalculated over the city's rolling six-month set after each update. The complete accumulated set remains stored.

A job newly discovered today may have an older `posted_at` value. "New this run" means newly added to this dataset, not necessarily advertised in the last 24 hours.

`posted_at` comes from the normalized ATS payload rather than the date Google discovered the URL. Workday uses `startDate`, Lever uses `createdAt`, Ashby uses `publishedAt`, and Greenhouse uses `first_published` when available with `updated_at` as a fallback. Daily posting activity should therefore be treated as an informative ATS-advertised-date trend, not a precise measurement of when every employer first created a requisition.

The six-month cutoff is inclusive and calendar-based. For example, on July 31 the active window begins January 31. API and frontend statistics are calculated dynamically from `postings`, so a job ages out of displayed statistics even if no new fetch runs that day.

## Local Setup

The project is currently developed with Python 3.13, but it uses standard Python/Flask APIs and should work on a recent Python 3 release.

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a `.env` file in the project root:

```dotenv
SERPER_API_KEY=your_serper_api_key
```

Obtain a key from [serper.dev](https://serper.dev/). Do not commit `.env`; it is ignored by Git.

### SSL note

Some corporate networks or local certificate configurations cause HTTPS verification failures. In that environment, use:

```bash
jobtrends-google-trends --insecure
```

`--insecure` disables TLS certificate verification and should only be used when necessary. Appending `2>/dev/null` hides SSL warnings, but it also hides real errors and is not recommended for routine debugging.

## Running the Data Pipeline

Update all tracked cities:

```bash
jobtrends-google-trends --insecure
```

Update selected cities:

```bash
jobtrends-google-trends \
  --cities "new york,san francisco,toronto" \
  --insecure
```

Useful options:

```text
--sites                 Comma-separated discovery domains
--since-date            Initial-fetch floor for cities not already in the DB
--title-include         Required title phrases
--title-exclude         Rejected title phrases
--max-pages             Maximum Serper pages per site-query batch
--site-batch-size       Number of domains combined into each Google query
--no-city-filter        Trust Google's city match without local validation
--no-postings           Store aggregate counts without full posting rows
--no-db                 Write exports only; disables incremental DB behavior
--db-path               Use a specific SQLite file
```

The default tracked set contains 24 cities:

```text
Atlanta, Austin, Boston, Chicago, Dallas, Denver, Houston,
Los Angeles, Memphis, Miami, Minneapolis, New York, Philadelphia,
Phoenix, Portland, Raleigh, Salt Lake City, San Diego, San Francisco,
San Jose, Seattle, St. Louis, Toronto, Washington DC
```

Edit `DEFAULT_CITIES` in `src/jobtrends/analysis/google_language_trends.py` and add coordinates to `CITY_COORDS` in `src/jobtrends/web/app.py` when adding a city permanently.

## Outputs and Statistics

Pipeline runs write only to the SQLite database; the web app and stats CLI read from it.

Print statistics directly from SQLite:

```bash
jobtrends-stats
jobtrends-stats --top 5 --titles 20
```

Job titles are counted as exact strings. Similar titles are intentionally not normalized yet.

## Running the Frontend

Start the Flask development server:

```bash
jobtrends-web
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in a browser. Keep the terminal process running while using the app.

Use another port if necessary:

```bash
jobtrends-web --port 8000
```

The frontend provides:

- A Leaflet map with posting totals for each city
- A shared date-range control with 1D, 7D, 6M, 12M, YTD, ALL, and custom dates; 6M remains the default. The inclusive 1D range covers yesterday through today.
- An overall language ranking and job-board breakdown (how many postings came from each ATS)
- City modals with local language rankings, a per-city job-board breakdown, and underlying job links
- Collapsed overall and per-city daily posting activity charts for the latest 30 days
- Per-city CSV downloads containing the past six months of job details

API routes:

```text
GET /
GET /api/stats
GET /api/cities
GET /api/city/<name>
GET /api/city/<name>/postings.csv
```

The JSON and CSV endpoints accept the same optional date-range query parameters used by the frontend:

```text
?range=1d
?range=7d
?range=6m
?range=12m
?range=ytd
?range=all
?range=custom&start=2026-07-01&end=2026-07-31
```

Date bounds are inclusive and interpreted as UTC calendar dates. Omitting `range` uses the rolling six-month default. Changing the frontend selection updates map totals, overall statistics, city details, posting activity, posting lists, and CSV downloads together; retained database history is not modified.

Leaflet and the CARTO basemap are loaded from external services, so the map requires internet access even when Flask is running locally.

## Optional Fixed-Company Pipeline

`language_trends.py` (`jobtrends-trends`) is an alternative data source. Instead of Google discovery, it crawls the employer boards listed in `config/ats_companies.json` through the same public ATS APIs.

```bash
jobtrends-trends --insecure
```

This path avoids Serper costs and is useful for methodological comparisons, but its coverage is limited to known employers. It writes rows with `source='ats'`; the web app currently displays only `source='google'`.

Use `jobtrends-discover` (`src/jobtrends/sources/discover_ats_companies.py`) when intentionally expanding the fixed employer list. It consumes Serper quota and is not part of the normal daily update.

## Production Configuration

The app is prepared to run behind Gunicorn:

```bash
gunicorn jobtrends.web.app:app
```

Relevant environment variables:

| Variable | Purpose |
|---|---|
| `SERPER_API_KEY` | Required by discovery/update jobs. |
| `JOB_TRENDS_DB` | SQLite path; use a persistent mounted disk in production. |
| `HOST` | Host used by `jobtrends-web`; defaults to `127.0.0.1`. |
| `PORT` | Port used by `jobtrends-web`; defaults to `5000`. |

A production deployment needs both:

1. A web service running `gunicorn jobtrends.web.app:app`.
2. A scheduled job running `jobtrends-google-trends` against the same persistent database.

### Daily macOS update

Install or refresh the local cron entry with:

```bash
./scripts/install_daily_cron.sh
```

The entry runs the incremental update every morning at 7:00 AM in the
computer's local time:

```cron
0 7 * * * /Users/ecarlso2/Projects/job-searching/scripts/run_daily_update.sh >> /Users/ecarlso2/Projects/job-searching/data/daily_update.log 2>&1
```

The wrapper prevents overlapping updates and records start, completion, and
pipeline output in `data/daily_update.log`. The Mac must be awake at the
scheduled time; traditional cron does not replay jobs missed while asleep.
If macOS reports `Operation not permitted`, grant the Terminal application Full
Disk Access in System Settings > Privacy & Security, then rerun the installer.

SQLite is appropriate for one web instance and one coordinated update process. Before horizontally scaling the web or worker tier, migrate the shared state to a server database such as PostgreSQL.

### Deploying to Render

`render.yaml` in the repo root is a [Blueprint](https://render.com/docs/blueprint-spec) that deploys a single always-on web service (no cold start) with a persistent disk:

1. In the Render dashboard, confirm the cheapest always-on web service plan/price and update `plan:` in `render.yaml` if it has changed, then create a Blueprint from this repo.
2. Set the `SERPER_API_KEY` secret in the service's environment tab (marked `sync: false` in `render.yaml`, so Render prompts for it instead of storing it in git).
3. The blueprint mounts a 1 GB disk at `/var/data` and points `JOB_TRENDS_DB` there so the database survives deploys and restarts. `config/ats_companies.json` is left on the default path — it's static and redeployed from git each time, not runtime-mutated.
4. Seed the disk with your existing data before (or right after) the first deploy: open a shell on the service (Render dashboard > Shell) and copy your local `job_trends.db` up to `/var/data/` (e.g. `scp`, or Render's shell file upload), otherwise the app starts from an empty database.
5. Only one worker is used (`gunicorn --workers 1`) — SQLite here assumes one writer, and Render disks attach to a single service instance.

Render's persistent disks aren't shared across separate services, so a separate "Cron Job" resource for the daily update wouldn't have access to this service's disk. Instead, the update runs **in-process**: `src/jobtrends/web/scheduler.py` starts a background thread (enabled via `JOBTRENDS_ENABLE_SCHEDULER=1`, set in `render.yaml`) that runs `jobtrends-google-trends --insecure` once a day at `JOBTRENDS_UPDATE_HOUR_UTC` (default 11:00 UTC). This only runs when that env var is set, so local `jobtrends-web` usage is unaffected and keeps relying on the macOS cron described above.

## Known Limitations

- Google/Serper results are ranked and capped, so the dataset is not exhaustive.
- Combining four domains into one query conserves quota but can reduce per-platform visibility.
- Google may surface a posting days after its advertised date.
- Closed and older-than-six-month jobs remain in the accumulated historical dataset but are excluded from current statistics once their advertised date crosses the rolling cutoff.
- City and title filtering are heuristic. Workday uses structured-location-only matching; other platforms may also use description text to establish city relevance.
- Language detection is phrase-based. It favors understandable, reproducible rules over natural-language inference and intentionally uses conservative phrases for ambiguous names such as Go and R.
- A posting can mention multiple languages, so language percentages do not sum to 100%.
- ATS endpoints are public implementation details and may change without notice.

## Safe Development Practices

- Do not delete `job_trends.db` casually; it contains the accumulated posting history.
- Test a new platform adapter with a platform-only `--sites` run before enabling it by default.
- Inspect accepted locations and URLs, not only aggregate counts.
- Immediately repeat a platform test and verify it adds zero rows; this confirms stable deduplication.
- Keep title and language rules centralized in `ats_job_search.py` and `language_detect.py` so both pipelines remain comparable.
