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
import os

from flask import Flask, jsonify, render_template

from trends_db import DEFAULT_DB_PATH, ensure_schema, get_connection, get_postings
from trends_stats import build_stats_data

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
    "miami": (25.7617, -80.1918),
    "minneapolis": (44.9778, -93.2650),
    "new york": (40.7128, -74.0060),
    "phoenix": (33.4484, -112.0740),
    "portland": (45.5152, -122.6784),
    "raleigh": (35.7796, -78.6382),
    "san diego": (32.7157, -117.1611),
    "san francisco": (37.7749, -122.4194),
    "san jose": (37.3382, -121.8863),
    "seattle": (47.6062, -122.3321),
    "toronto": (43.6532, -79.3832),
    "washington dc": (38.9072, -77.0369),
}

app = Flask(__name__)


def _connect():
    conn = get_connection(DEFAULT_DB_PATH)
    ensure_schema(conn)
    return conn


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    """Overall running statistics (summary, top languages, top titles)."""
    conn = _connect()
    try:
        data = build_stats_data(conn, source=SOURCE, top=15, titles=10)
    finally:
        conn.close()
    return jsonify(data)


@app.route("/api/cities")
def api_cities():
    """Tracked cities with coordinates and headline totals (for the map pins)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT city, total_matched, updated_at_utc FROM cities "
            "WHERE source = ? ORDER BY city",
            (SOURCE,),
        ).fetchall()
    finally:
        conn.close()

    cities = []
    for city, total, updated in rows:
        coords = CITY_COORDS.get(city)
        cities.append(
            {
                "city": city,
                "label": city.title(),
                "total_matched": total,
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
    conn = _connect()
    try:
        meta = conn.execute(
            "SELECT total_matched, since_date, updated_at_utc FROM cities "
            "WHERE city = ? AND source = ?",
            (key, SOURCE),
        ).fetchone()
        lang_rows = conn.execute(
            "SELECT language, rank, count, percent FROM city_language_counts "
            "WHERE city = ? AND source = ? AND count > 0 ORDER BY rank",
            (key, SOURCE),
        ).fetchall()
        postings = get_postings(conn, city=key, source=SOURCE)
    finally:
        conn.close()

    if meta is None:
        return jsonify({"error": f"city {name!r} not found"}), 404

    postings_sorted = sorted(postings, key=lambda p: p.get("posted_at") or "", reverse=True)
    return jsonify(
        {
            "city": key,
            "label": key.title(),
            "total_matched": meta[0],
            "since_date": meta[1],
            "updated_at": meta[2],
            "languages": [
                {"language": lang, "rank": rank, "count": count, "percent": percent}
                for lang, rank, count, percent in lang_rows
            ],
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
