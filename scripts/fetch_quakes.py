"""
fetch_quakes.py
---------------
Fetches seismic event data from the USGS FDSN Event Web Service,
merges new events into the local GeoJSON accumulative file and saves
the result only when new events are detected.

Data source : https://earthquake.usgs.gov/fdsnws/event/1/
License     : USGS data is in the public domain.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USGS_ENDPOINT: str = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# Bounding box for the Canary Islands (decimal degrees)
BBOX: dict[str, float] = {
    "minlatitude": 27.0,
    "maxlatitude": 30.0,
    "minlongitude": -19.0,
    "maxlongitude": -13.0,
}

MIN_MAGNITUDE: float = 0.0
HISTORY_START: str = "2015-01-01"

# Overlap window to avoid missing events near the query boundary
OVERLAP_MINUTES: int = 10

# Where the accumulative GeoJSON is stored (relative to repo root)
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OUTPUT_FILE: Path = REPO_ROOT / "data" / "sismos.json"

# HTTP settings
REQUEST_TIMEOUT: int = 30  # seconds
MAX_RETRIES: int = 3
RETRY_BACKOFF: int = 5  # seconds between retries

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def load_existing(path: Path) -> dict[str, Any]:
    """Load the existing GeoJSON FeatureCollection, or return an empty one."""
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            log.warning("Could not parse %s (%s). Starting fresh.", path, exc)
    return {"type": "FeatureCollection", "features": []}


def save(path: Path, collection: dict[str, Any]) -> None:
    """Persist the GeoJSON FeatureCollection to disk (minified)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(collection, fh, ensure_ascii=False, separators=(",", ":"))
    log.info("Saved %d features → %s", len(collection["features"]), path)


# ---------------------------------------------------------------------------
# USGS API
# ---------------------------------------------------------------------------


def fetch_usgs(starttime: str, endtime: str) -> list[dict[str, Any]]:
    """
    Query the USGS FDSN Event Web Service and return a list of GeoJSON features.

    Retries up to MAX_RETRIES times with exponential-ish back-off on transient
    errors (network failures, HTTP 5xx, HTTP 429 rate-limit).
    """
    params: dict[str, Any] = {
        "format": "geojson",
        "starttime": starttime,
        "endtime": endtime,
        "minmagnitude": MIN_MAGNITUDE,
        "orderby": "time-asc",
        **BBOX,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(
                "Fetching USGS (attempt %d/%d) — window: %s → %s",
                attempt,
                MAX_RETRIES,
                starttime,
                endtime,
            )
            response = requests.get(
                USGS_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            features: list[dict[str, Any]] = data.get("features", [])
            log.info("Received %d features from USGS.", len(features))
            return features

        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            log.error("HTTP error %d: %s", status_code, exc)
            if status_code == 429:
                wait = RETRY_BACKOFF * attempt * 2
                log.warning("Rate limited. Waiting %ds before retry…", wait)
                time.sleep(wait)
                continue

        except requests.exceptions.RequestException as exc:
            log.error("Request failed: %s", exc)

        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF * attempt
            log.info("Retrying in %ds…", wait)
            time.sleep(wait)

    log.error("All %d attempts failed. Returning empty list.", MAX_RETRIES)
    return []


# ---------------------------------------------------------------------------
# Merge / deduplication
# ---------------------------------------------------------------------------


def merge(
    existing: dict[str, Any],
    incoming: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """
    Merge incoming features into the existing FeatureCollection.

    Deduplicates by the feature 'id' field (USGS event ID, e.g. 'us7000abc1').
    Returns the merged collection and the count of newly added events.
    """
    seen_ids: set[str] = {f["id"] for f in existing["features"] if "id" in f}
    new_count: int = 0

    for feature in incoming:
        event_id: str | None = feature.get("id")
        if event_id and event_id not in seen_ids:
            existing["features"].append(feature)
            seen_ids.add(event_id)
            new_count += 1

    # Keep features sorted chronologically (oldest first)
    existing["features"].sort(
        key=lambda f: f.get("properties", {}).get("time") or 0
    )

    return existing, new_count


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------


def determine_time_window(existing: dict[str, Any]) -> tuple[str, str]:
    """
    Determine the query window for USGS.

    - First run  : fetch the full history from HISTORY_START.
    - Subsequent : fetch from (latest known event − OVERLAP_MINUTES) to now.
    """
    now: datetime = datetime.now(tz=timezone.utc)
    endtime: str = now.strftime("%Y-%m-%dT%H:%M:%S")

    features: list[dict[str, Any]] = existing.get("features", [])
    if not features:
        log.info("No existing data. Full historical fetch from %s.", HISTORY_START)
        return HISTORY_START, endtime

    timestamps: list[int] = [
        f["properties"]["time"]
        for f in features
        if f.get("properties", {}).get("time") is not None
    ]

    if not timestamps:
        return HISTORY_START, endtime

    latest_ms: int = max(timestamps)
    latest_dt: datetime = datetime.fromtimestamp(latest_ms / 1000, tz=timezone.utc)
    overlap_dt: datetime = latest_dt - timedelta(minutes=OVERLAP_MINUTES)
    starttime: str = overlap_dt.strftime("%Y-%m-%dT%H:%M:%S")

    log.info(
        "Latest stored event: %s. Query from %s (-%d min overlap).",
        latest_dt.isoformat(),
        starttime,
        OVERLAP_MINUTES,
    )
    return starttime, endtime


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=== sismocan / fetch_quakes.py ===")

    existing: dict[str, Any] = load_existing(OUTPUT_FILE)
    log.info("Existing features in store: %d", len(existing.get("features", [])))

    starttime, endtime = determine_time_window(existing)
    incoming: list[dict[str, Any]] = fetch_usgs(starttime, endtime)

    if not incoming:
        log.info("No data received from USGS. Nothing to update.")
        return

    merged, new_count = merge(existing, incoming)

    if new_count == 0:
        log.info("No new events detected. Skipping write.")
        return

    log.info("New events added: %d. Total in store: %d.", new_count, len(merged["features"]))
    save(OUTPUT_FILE, merged)
    log.info("Done.")


if __name__ == "__main__":
    main()
