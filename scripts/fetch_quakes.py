"""
fetch_quakes.py — sismocan orchestrator
----------------------------------------
Coordinates data ingestion from all configured sources (USGS, IGN),
merges new events into the local GeoJSON accumulative store and saves
the result only when new events are detected.

Sources
-------
  - USGS FDSN Event Web Service  (public domain, REST/GeoJSON)
  - IGN  Catálogo de Terremotos  (Liferay portlet download endpoint)

Usage
-----
  python scripts/fetch_quakes.py

  Intended to be run by GitHub Actions on a cron schedule.
"""

from __future__ import annotations

import json
import logging
import sys
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Add the scripts/ directory to the path so relative imports work both when
# executed directly and when imported from tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.ign import fetch_ign
from sources.usgs import fetch_usgs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HISTORY_START: str = "2015-01-01"

# USGS no indexa Canarias fiablemente por debajo de M1.5
# Los eventos pequeños vienen de IGN

# Look back slightly further than the last event to catch late-arriving data
OVERLAP_MINUTES: int = 10

# For IGN historical fetch, split into monthly chunks to respect server limits
IGN_CHUNK_MONTHS: int = 1

# Where the accumulative GeoJSON is stored (relative to repo root)
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OUTPUT_FILE: Path = REPO_ROOT / "data" / "sismos.json"

# Proximity thresholds for cross-source deduplication
DEDUP_LAT_LON_DELTA: float = 0.15   # degrees  (~15 km)
DEDUP_TIME_DELTA_MS: int   = 120_000 # 2 minutes in milliseconds

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
    log.info("Saved %d features -> %s", len(collection["features"]), path)


# ---------------------------------------------------------------------------
# Time window helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _latest_timestamp(features: list[dict[str, Any]]) -> int | None:
    """Return the maximum time (epoch ms) across all features, or None."""
    timestamps = [
        f["properties"]["time"]
        for f in features
        if isinstance(f.get("properties", {}).get("time"), (int, float))
    ]
    return int(max(timestamps)) if timestamps else None


def determine_usgs_window(existing: dict[str, Any]) -> tuple[str, str]:
    """
    Return (starttime, endtime) for the USGS incremental query.

    First run  → full history from HISTORY_START.
    Subsequent → from (latest event − OVERLAP_MINUTES) to now.
    """
    endtime = _now_iso()
    features = existing.get("features", [])
    latest_ms = _latest_timestamp(features)

    if latest_ms is None:
        log.info("[USGS] No existing data. Full historical fetch from %s.", HISTORY_START)
        return HISTORY_START, endtime

    latest_dt  = datetime.fromtimestamp(latest_ms / 1000, tz=timezone.utc)
    overlap_dt = latest_dt - timedelta(minutes=OVERLAP_MINUTES)
    starttime  = overlap_dt.strftime("%Y-%m-%dT%H:%M:%S")
    log.info(
        "[USGS] Latest stored: %s. Query from %s (-%d min overlap).",
        latest_dt.isoformat(), starttime, OVERLAP_MINUTES,
    )
    return starttime, endtime


def _monthly_windows(start_iso: str, end_iso: str) -> list[tuple[str, str]]:
    """
    Split [start_iso, end_iso] into monthly (DD/MM/YYYY, DD/MM/YYYY) chunks
    suitable for the IGN form endpoint.
    """
    start = datetime.strptime(start_iso[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end   = datetime.strptime(end_iso[:10],   "%Y-%m-%d").replace(tzinfo=timezone.utc)

    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        month_last_day = monthrange(cursor.year, cursor.month)[1]
        chunk_end = cursor.replace(day=month_last_day)
        if chunk_end > end:
            chunk_end = end
        windows.append(
            (cursor.strftime("%d/%m/%Y"), chunk_end.strftime("%d/%m/%Y"))
        )
        # Advance to the first day of the next month
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1, day=1)

    return windows


def determine_ign_windows(existing: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Return a list of (start_date, end_date) windows (DD/MM/YYYY) for IGN.

    First run  → monthly chunks from HISTORY_START to today.
    Subsequent → single window covering the last OVERLAP_MINUTES + a few days.
    """
    # Filter to IGN-sourced features only
    ign_features = [
        f for f in existing.get("features", [])
        if f.get("properties", {}).get("source") == "ign"
    ]
    latest_ms = _latest_timestamp(ign_features)
    today_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    if latest_ms is None:
        log.info("[IGN] No existing IGN data. Full historical fetch from %s.", HISTORY_START)
        return _monthly_windows(HISTORY_START, today_iso)

    latest_dt  = datetime.fromtimestamp(latest_ms / 1000, tz=timezone.utc)
    overlap_dt = latest_dt - timedelta(minutes=OVERLAP_MINUTES)
    # Use a single window for the incremental case
    start_ddmmyyyy = overlap_dt.strftime("%d/%m/%Y")
    end_ddmmyyyy   = datetime.now(tz=timezone.utc).strftime("%d/%m/%Y")
    log.info("[IGN] Incremental window: %s -> %s", start_ddmmyyyy, end_ddmmyyyy)
    return [(start_ddmmyyyy, end_ddmmyyyy)]


# ---------------------------------------------------------------------------
# Merge / deduplication
# ---------------------------------------------------------------------------


def merge_by_id(
    existing: dict[str, Any],
    incoming: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """
    Merge incoming features using feature 'id' as the deduplication key.

    Returns the updated collection and the count of newly added features.
    """
    seen_ids: set[str] = {f["id"] for f in existing["features"] if f.get("id")}
    new_count = 0

    for feature in incoming:
        fid = feature.get("id")
        if fid and fid not in seen_ids:
            existing["features"].append(feature)
            seen_ids.add(fid)
            new_count += 1

    return existing, new_count


def deduplicate_cross_source(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Remove near-duplicate events reported by multiple sources.

    Two features are considered duplicates when they are spatially and
    temporally proximate (within DEDUP_LAT_LON_DELTA degrees and
    DEDUP_TIME_DELTA_MS milliseconds). When a duplicate pair is found,
    the IGN record is preferred over USGS as the local network has
    finer-grained resolution for Canarian events.

    Complexity: O(n²) — acceptable for the expected dataset size (~10k events).
    """
    if len(features) <= 1:
        return features

    # Sort chronologically for the early-exit optimisation on time delta
    sorted_f = sorted(
        features, key=lambda f: f.get("properties", {}).get("time") or 0
    )
    n = len(sorted_f)
    skip = [False] * n

    for i in range(n):
        if skip[i]:
            continue
        pi     = sorted_f[i].get("properties") or {}
        ci     = sorted_f[i].get("geometry", {}).get("coordinates") or []
        ti     = pi.get("time") or 0
        src_i  = pi.get("source", "")
        if len(ci) < 2:
            continue

        for j in range(i + 1, n):
            if skip[j]:
                continue
            pj    = sorted_f[j].get("properties") or {}
            cj    = sorted_f[j].get("geometry", {}).get("coordinates") or []
            tj    = pj.get("time") or 0
            src_j = pj.get("source", "")
            if len(cj) < 2:
                continue

            # Early exit: list is time-sorted so no later entry can match
            if abs(ti - tj) > DEDUP_TIME_DELTA_MS:
                break

            # Spatial proximity check
            if (
                abs(ci[1] - cj[1]) < DEDUP_LAT_LON_DELTA
                and abs(ci[0] - cj[0]) < DEDUP_LAT_LON_DELTA
            ):
                # Prefer IGN; if both same source, keep the first (i)
                if src_j == "ign" and src_i != "ign":
                    skip[i] = True
                    break
                else:
                    skip[j] = True

    kept = [f for idx, f in enumerate(sorted_f) if not skip[idx]]
    removed = n - len(kept)
    if removed:
        log.info("Cross-source dedup removed %d duplicate(s).", removed)
    return kept


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=== sismocan / fetch_quakes.py ===")

    existing = load_existing(OUTPUT_FILE)
    total_before = len(existing.get("features", []))
    log.info("Features in store before update: %d", total_before)

    new_total = 0

    # ------------------------------------------------------------------
    # Source 1: USGS
    # ------------------------------------------------------------------
    usgs_start, usgs_end = determine_usgs_window(existing)
    usgs_features = fetch_usgs(usgs_start, usgs_end)
    if usgs_features:
        existing, added = merge_by_id(existing, usgs_features)
        log.info("[USGS] New features added: %d", added)
        new_total += added

    # ------------------------------------------------------------------
    # Source 2: IGN
    # ------------------------------------------------------------------
    ign_windows = determine_ign_windows(existing)
    ign_added_total = 0
    for start_dmy, end_dmy in ign_windows:
        ign_features = fetch_ign(start_dmy, end_dmy)
        if ign_features:
            existing, added = merge_by_id(existing, ign_features)
            ign_added_total += added

    log.info("[IGN] New features added (all windows): %d", ign_added_total)
    new_total += ign_added_total

    # ------------------------------------------------------------------
    # Cross-source deduplication
    # ------------------------------------------------------------------
    if new_total > 0:
        before_dedup = len(existing["features"])
        existing["features"] = deduplicate_cross_source(existing["features"])
        # Sort chronologically (oldest first)
        existing["features"].sort(
            key=lambda f: f.get("properties", {}).get("time") or 0
        )
        after_dedup = len(existing["features"])
        net_new = after_dedup - total_before
        log.info(
            "Store: %d -> %d (net new: %d, dedup removed: %d).",
            total_before, after_dedup, net_new, before_dedup - after_dedup,
        )
        save(OUTPUT_FILE, existing)
    else:
        log.info("No new events from any source. Skipping write.")

    log.info("Done.")


if __name__ == "__main__":
    main()

