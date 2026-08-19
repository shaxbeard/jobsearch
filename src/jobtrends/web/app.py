#!/usr/bin/env python3
"""Flask frontend for the job-trends database.

Serves a single-page app: a map of the USA (plus Toronto) with a pin beside
each tracked city, an overall-stats panel, and a per-city modal (opened by
clicking a pin) backed by the stored postings for future drill-down.

Run:
  python app.py            # then open http://127.0.0.1:5000
  python app.py --port 8000
"""

from __future__ import annotations

import argparse
import csv
import io
import os
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from flask import Flask, Response, jsonify, render_template, request

from jobtrends.language_detect import count_from_matched_languages, count_tools, rank_languages, rank_tools
from jobtrends.trends_db import (
    DEFAULT_DB_PATH,
    STATS_WINDOW_MONTHS,
    ensure_schema,
    get_connection,
    get_postings_in_date_range,
)
from jobtrends.analysis.trends_stats import (
    build_daily_posting_counts,
    build_monthly_posting_counts,
    build_stats_data,
)

# Which stored data the frontend reads. The Google-discovery pipeline is the
# one currently populating the database.
SOURCE = "google"

# Approximate (lat, lng) for each city we may track, so pins land in the right
# spot. Cities without an entry still appear in stats but get no map pin.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "atlanta": (33.7490, -84.3880),
    "austin": (30.2672, -97.7431),
    "boston": (42.3601, -71.0589),
    "chicago": (41.8781, -87.6298),
    "dallas": (32.7767, -96.7970),
    "denver": (39.7392, -104.9903),
    "houston": (29.7604, -95.3698),
    "los angeles": (34.0522, -118.2437),
    "memphis": (35.1495, -90.0490),
    "miami": (25.7617, -80.1918),
    "minneapolis": (44.9778, -93.2650),
    "new york": (40.7128, -74.0060),
    "philadelphia": (39.9526, -75.1652),
    "phoenix": (33.4484, -112.0740),
    "portland": (45.5152, -122.6784),
    "raleigh": (35.7796, -78.6382),
    "salt lake city": (40.7608, -111.8910),
    "san diego": (32.7157, -117.1611),
    "san francisco": (37.7749, -122.4194),
    "san jose": (37.3382, -121.8863),
    "seattle": (47.6062, -122.3321),
    "st louis": (38.6270, -90.1994),
    "toronto": (43.6532, -79.3832),
    "washington dc": (38.9072, -77.0369),
}

app = Flask(__name__)


def _connect():
    conn = get_connection(DEFAULT_DB_PATH)
    ensure_schema(conn)
    return conn


def _csv_cell(value: object) -> str:
    """Keep exported text from being interpreted as a spreadsheet formula."""
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _subtract_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return day.replace(year=year, month=month, day=min(day.day, monthrange(year, month)[1]))


def _date_range_from_request() -> tuple[str, str, str, str]:
    """Resolve the requested inclusive UTC date range and its display label."""
    today = datetime.now(timezone.utc).date()
    range_key = request.args.get("range", "6m").strip().lower()
    presets = {
        "1d": (today - timedelta(days=1), "Past day"),
        "7d": (today - timedelta(days=6), "Past 7 days"),
        "6m": (_subtract_months(today, 6), "Past 6 months"),
        "12m": (_subtract_months(today, 12), "Past 12 months"),
        "ytd": (today.replace(month=1, day=1), "Year to date"),
    }
    if range_key == "all":
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT MIN(date(posted_at)) FROM postings WHERE source = ?",
                (SOURCE,),
            ).fetchone()
        finally:
            conn.close()
        start = date.fromisoformat(row[0]) if row and row[0] else today
        return start.isoformat(), today.isoformat(), range_key, "All time"
    if range_key == "custom":
        try:
            start = date.fromisoformat(request.args.get("start", ""))
            end = date.fromisoformat(request.args.get("end", ""))
        except ValueError as exc:
            raise ValueError("Custom ranges require valid start and end dates") from exc
        if start > end:
            raise ValueError("Custom range start date must not be after its end date")
        if end > today:
            raise ValueError("Custom range end date must not be in the future")
        return start.isoformat(), end.isoformat(), range_key, f"{start:%b %-d, %Y} – {end:%b %-d, %Y}"
    if range_key not in presets:
        raise ValueError(f"Unknown date range: {range_key}")
    start, label = presets[range_key]
    return start.isoformat(), today.isoformat(), range_key, label


@app.errorhandler(ValueError)
def handle_value_error(error: ValueError):
    return jsonify({"error": str(error)}), 400


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    """Overall running statistics (summary, top languages, top titles)."""
    start_date, end_date, range_key, range_label = _date_range_from_request()
    conn = _connect()
    try:
        data = build_stats_data(
            conn,
            source=SOURCE,
            top=15,
            titles=10,
            start_date=start_date,
            end_date=end_date,
            range_key=range_key,
            range_label=range_label,
        )
    finally:
        conn.close()
    return jsonify(data)


@app.route("/api/cities")
def api_cities():
    """Tracked cities with coordinates and headline totals (for the map pins)."""
    start_date, end_date, _, _ = _date_range_from_request()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT city, updated_at_utc FROM cities "
            "WHERE source = ? ORDER BY city",
            (SOURCE,),
        ).fetchall()
        recent_totals = {
            city: len(
                get_postings_in_date_range(
                    conn,
                    city=city,
                    source=SOURCE,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            for city, _ in rows
        }
    finally:
        conn.close()

    cities = []
    for city, updated in rows:
        coords = CITY_COORDS.get(city)
        cities.append(
            {
                "city": city,
                "label": city.title(),
                "total_matched": recent_totals[city],
                "updated_at": updated,
                "lat": coords[0] if coords else None,
                "lng": coords[1] if coords else None,
            }
        )
    return jsonify(cities)


@app.route("/api/city/<name>")
def api_city(name: str):
    """Full detail for one city: language breakdown + postings (drill-down)."""
    key = name.strip().lower()
    start_date, end_date, range_key, range_label = _date_range_from_request()
    conn = _connect()
    try:
        meta = conn.execute(
            "SELECT since_date, updated_at_utc FROM cities "
            "WHERE city = ? AND source = ?",
            (key, SOURCE),
        ).fetchone()
        postings = get_postings_in_date_range(
            conn, city=key, source=SOURCE, start_date=start_date, end_date=end_date
        )
    finally:
        conn.close()

    if meta is None:
        return jsonify({"error": f"city {name!r} not found"}), 404

    counts = count_from_matched_languages([p["matched_languages"] for p in postings])
    languages = [row for row in rank_languages(counts, len(postings)) if row["count"] > 0]
    tool_counts = count_tools(postings)
    tools = [row for row in rank_tools(tool_counts, len(postings)) if row["count"] > 0]
    postings_sorted = sorted(postings, key=lambda p: p.get("posted_at") or "", reverse=True)
    return jsonify(
        {
            "city": key,
            "label": key.title(),
            "total_matched": len(postings),
            "since_date": meta[0],
            "updated_at": meta[1],
            "window_months": STATS_WINDOW_MONTHS,
            "range_key": range_key,
            "range_label": range_label,
            "start_date": start_date,
            "end_date": end_date,
            "cutoff_date": start_date,
            "posting_activity_daily": build_daily_posting_counts(
                postings, cutoff=start_date, through=date.fromisoformat(end_date)
            ),
            "posting_activity_monthly": build_monthly_posting_counts(
                postings, cutoff=start_date, through=date.fromisoformat(end_date)
            ),
            "languages": languages,
            "tools": tools,
            "postings": [
                {
                    "company": p["company"],
                    "title": p["title"],
                    "location": p["location"],
                    "url": p["url"],
                    "posted_at": p["posted_at"],
                    "matched_languages": p["matched_languages"],
                }
                for p in postings_sorted
            ],
        }
    )


@app.route("/api/city/<name>/postings.csv")
def download_city_postings(name: str):
    """Download one city's postings from the rolling stats window as CSV."""
    key = name.strip().lower()
    start_date, end_date, range_key, _ = _date_range_from_request()
    conn = _connect()
    try:
        city_exists = conn.execute(
            "SELECT 1 FROM cities WHERE city = ? AND source = ?",
            (key, SOURCE),
        ).fetchone()
        postings = get_postings_in_date_range(
            conn, city=key, source=SOURCE, start_date=start_date, end_date=end_date
        )
    finally:
        conn.close()

    if city_exists is None:
        return jsonify({"error": f"city {name!r} not found"}), 404

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["Company Name", "Job Title", "Job City/Cities", "Posting Date", "Languages", "Job URL"]
    )
    for posting in sorted(postings, key=lambda row: row.get("posted_at") or "", reverse=True):
        writer.writerow(
            [
                _csv_cell(posting["company"]),
                _csv_cell(posting["title"]),
                _csv_cell(posting["location"]),
                _csv_cell(posting["posted_at"]),
                _csv_cell(", ".join(posting["matched_languages"])),
                _csv_cell(posting["url"]),
            ]
        )

    filename = f"{key.replace(' ', '-')}-job-postings-{range_key}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Host to bind (default: 127.0.0.1, or $HOST). Use 0.0.0.0 in containers.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "5000")),
        help="Port to serve on (default: 5000, or $PORT).",
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug/reloader.")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
