"""
usgs.py — USGS Earthquake Hazards Program source
-------------------------------------------------
Wraps the FDSN Event Web Service (GeoJSON endpoint).
Data is in the public domain (USGS = US federal agency).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USGS_ENDPOINT: str = "https://earthquake.usgs.gov/fdsnws/event/1/query"

BBOX: dict[str, float] = {
    "minlatitude":  27.0,
    "maxlatitude":  30.0,
    "minlongitude": -19.0,
    "maxlongitude": -13.0,
}

MIN_MAGNITUDE: float = 0.0

REQUEST_TIMEOUT: int = 30
MAX_RETRIES: int = 3
RETRY_BACKOFF: int = 5

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_usgs(starttime: str, endtime: str) -> list[dict[str, Any]]:
    """
    Query the USGS FDSN Event Web Service and return raw GeoJSON features.

    Features are already in the sismocan unified schema (USGS is the
    reference format), but each feature is tagged with source = 'usgs'
    so the merge layer can identify its origin.

    Parameters
    ----------
    starttime : str  ISO date/datetime  (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    endtime   : str  ISO date/datetime

    Returns
    -------
    list of GeoJSON feature dicts (may be empty on failure or no results).
    """
    params: dict[str, Any] = {
        "format":      "geojson",
        "starttime":   starttime,
        "endtime":     endtime,
        "minmagnitude": MIN_MAGNITUDE,
        "orderby":     "time-asc",
        **BBOX,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(
                "[USGS] Fetching (attempt %d/%d) -- window: %s -> %s",
                attempt, MAX_RETRIES, starttime, endtime,
            )
            resp = requests.get(
                USGS_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            features: list[dict[str, Any]] = data.get("features", [])

            # Tag each feature with its origin
            for f in features:
                if "properties" in f and f["properties"] is not None:
                    f["properties"].setdefault("source", "usgs")

            log.info("[USGS] Received %d features.", len(features))
            return features

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            log.error("[USGS] HTTP %d: %s", status, exc)
            if status == 429:
                wait = RETRY_BACKOFF * attempt * 2
                log.warning("[USGS] Rate limited. Waiting %ds…", wait)
                time.sleep(wait)
                continue

        except requests.exceptions.RequestException as exc:
            log.error("[USGS] Request failed: %s", exc)

        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF * attempt
            log.info("[USGS] Retrying in %ds…", wait)
            time.sleep(wait)

    log.error("[USGS] All %d attempts failed.", MAX_RETRIES)
    return []
