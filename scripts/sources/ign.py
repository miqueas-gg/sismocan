"""
ign.py — IGN Catálogo de Terremotos source
-------------------------------------------
Fetches seismic data from the IGN Catálogo de Terremotos using the
undocumented (but stable) Liferay portlet download endpoint discovered
via DevTools.

Flow:
  1. GET the catalog page to obtain a valid JSESSIONID session cookie.
  2. POST the download form with tipoDescarga=geojson to retrieve data.

Notes:
  - The portlet prefix for all form fields is _PORTLET_PREFIX.
  - Dates must be in DD/MM/YYYY format.
  - Each feature is normalised to the USGS-compatible sismocan schema and
    tagged with properties.source = 'ign'.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Liferay portlet identifier prefix (all form fields share this prefix)
_PORTLET_PREFIX = (
    "_IGNSISCatalogoTerremotos_WAR_IGNSISCatalogoTerremotosportlet_"
)

IGN_CATALOG_URL = "https://www.ign.es/web/ign/portal/sis-catalogo-terremotos"
IGN_DOWNLOAD_URL = (
    "https://www.ign.es/web/ign/portal/sis-catalogo-terremotos"
    "?p_p_id=IGNSISCatalogoTerremotos_WAR_IGNSISCatalogoTerremotosportlet"
    "&p_p_lifecycle=2"
    "&p_p_state=normal"
    "&p_p_mode=view"
    "&p_p_cacheability=cacheLevelPage"
    "&p_p_col_id=column-1"
    "&p_p_col_count=1"
    f"&{_PORTLET_PREFIX}jspPage=%2Fjsp%2Fterremoto.jsp"
)

# Bounding box for the Canary Islands
BBOX: dict[str, str] = {
    "latMin": "26",
    "latMax": "31",
    "longMin": "-20",
    "longMax": "-12",
}

REQUEST_TIMEOUT: int = 45
MAX_RETRIES: int = 3
RETRY_BACKOFF: int = 5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _p(name: str) -> str:
    """Return the full portlet-prefixed form field name."""
    return f"{_PORTLET_PREFIX}{name}"


def _make_form_data(start_date: str, end_date: str) -> dict[str, str]:
    """
    Build the multipart form-data payload for the IGN download request.

    Parameters
    ----------
    start_date : str  DD/MM/YYYY
    end_date   : str  DD/MM/YYYY
    """
    now_ms = str(int(datetime.now(tz=timezone.utc).timestamp() * 1000))
    return {
        _p("formDate"):      now_ms,
        _p("fases"):         "no",
        _p("selIntensidad"): "N",
        _p("selMagnitud"):   "N",
        _p("selProf"):       "N",
        _p("latMin"):        BBOX["latMin"],
        _p("latMax"):        BBOX["latMax"],
        _p("longMin"):       BBOX["longMin"],
        _p("longMax"):       BBOX["longMax"],
        _p("startDate"):     start_date,
        _p("endDate"):       end_date,
        _p("intMin"):        "0",
        _p("intMax"):        "8",
        _p("magMin"):        "0",
        _p("magMax"):        "8",
        _p("cond"):          "",
        _p("profMin"):       "",
        _p("profMax"):       "",
        _p("tipoDescarga"):  "geojson",
    }


def _parse_epoch_ms(raw: Any) -> int | None:
    """
    Attempt to parse a time value into Unix epoch milliseconds.

    Handles:
      - int / float (already epoch ms or s)
      - ISO-8601 strings  (e.g. '2024-03-15T10:23:45')
      - Spanish date strings (e.g. '15/03/2024 10:23:45')
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Heuristic: if the value is plausibly seconds (< 2e10) convert to ms
        val = int(raw)
        return val * 1000 if val < 20_000_000_000 else val
    if isinstance(raw, str):
        raw = raw.strip()
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        ):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return None


def _normalize_feature(feature: dict[str, Any]) -> dict[str, Any] | None:
    """
    Normalise a raw IGN GeoJSON feature to the sismocan unified schema.

    The normalised schema mirrors the USGS GeoJSON properties so that the
    frontend and merge logic need not distinguish between sources.

    Returns None if the feature lacks minimum required fields (coordinates).
    """
    props = feature.get("properties") or {}
    geom  = feature.get("geometry")  or {}
    coords: list = geom.get("coordinates") or []

    if len(coords) < 2:
        return None

    # --- Magnitude ----------------------------------------------------------
    mag_raw = (
        props.get("magnitud")
        or props.get("mag")
        or props.get("magnitude")
    )
    mag = float(mag_raw) if mag_raw is not None else None

    # --- Depth --------------------------------------------------------------
    depth_raw = (
        props.get("profundidad")
        or props.get("depth")
        or (coords[2] if len(coords) >= 3 else None)
    )
    depth = float(depth_raw) if depth_raw is not None else None

    # --- Time ---------------------------------------------------------------
    # IGN may combine fecha + hora_utc, or use a single datetime field
    raw_time = (
        props.get("hora_utc")
        or props.get("fecha_hora")
        or props.get("time")
        or props.get("fecha")
    )
    # Some responses combine date and time in separate fields
    if raw_time is None:
        fecha = props.get("fecha", "")
        hora  = props.get("hora",  "")
        if fecha and hora:
            raw_time = f"{fecha} {hora}"

    epoch_ms = _parse_epoch_ms(raw_time)

    # --- Place / localisation -----------------------------------------------
    place = (
        props.get("localizacion")
        or props.get("lugar")
        or props.get("place")
        or ""
    )

    # --- Feature ID ---------------------------------------------------------
    raw_id = feature.get("id") or props.get("id") or props.get("evento")
    feature_id = (
        f"ign_{raw_id}"
        if raw_id and not str(raw_id).startswith("ign_")
        else (raw_id or f"ign_{coords[0]}_{coords[1]}_{epoch_ms}")
    )

    # --- Build normalised coordinates [lon, lat, depth?] -------------------
    norm_coords: list[float] = [float(coords[0]), float(coords[1])]
    if depth is not None:
        norm_coords.append(depth)

    return {
        "type": "Feature",
        "id": str(feature_id),
        "properties": {
            "mag":    mag,
            "place":  place,
            "time":   epoch_ms,
            "depth":  depth,
            "url":    props.get("url") or props.get("detail"),
            "source": "ign",
        },
        "geometry": {
            "type": "Point",
            "coordinates": norm_coords,
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_ign(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """
    Fetch and normalise IGN seismic events for the given date window.

    Parameters
    ----------
    start_date : str  DD/MM/YYYY
    end_date   : str  DD/MM/YYYY

    Returns
    -------
    list of normalised GeoJSON feature dicts (may be empty on failure).
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; sismocan-bot/1.0; "
                "+https://github.com/miqueas-gg/sismocan)"
            ),
            "Accept-Language": "es-ES,es;q=0.9",
        }
    )

    # ------------------------------------------------------------------
    # Step 1 — GET catalog page to acquire a valid JSESSIONID cookie
    # ------------------------------------------------------------------
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(
                "[IGN] Acquiring session (attempt %d/%d)…", attempt, MAX_RETRIES
            )
            resp = session.get(
                IGN_CATALOG_URL,
                headers={"Accept": "text/html,*/*;q=0.8"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            if "JSESSIONID" in session.cookies:
                log.info("[IGN] Session ready (JSESSIONID acquired).")
            else:
                log.warning("[IGN] JSESSIONID not present — proceeding anyway.")
            break
        except requests.RequestException as exc:
            log.error("[IGN] Session GET failed: %s", exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    else:
        log.error("[IGN] Could not acquire session. Aborting IGN fetch.")
        return []

    # ------------------------------------------------------------------
    # Step 2 — POST download request
    # ------------------------------------------------------------------
    form_data = _make_form_data(start_date, end_date)
    log.info("[IGN] Requesting GeoJSON for %s -> %s", start_date, end_date)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(
                IGN_DOWNLOAD_URL,
                data=form_data,
                headers={
                    "Accept":  "application/json, text/plain, */*",
                    "Referer": IGN_CATALOG_URL,
                    "Origin":  "https://www.ign.es",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            log.info(
                "[IGN] Response: %d bytes — Content-Type: %s",
                len(resp.content),
                content_type,
            )

            raw = resp.json()
            raw_features: list = raw.get("features", [])
            log.info("[IGN] Raw features received: %d", len(raw_features))

            normalised = [
                n
                for f in raw_features
                if (n := _normalize_feature(f)) is not None
            ]
            log.info("[IGN] Normalised features: %d", len(normalised))
            return normalised

        except (requests.RequestException, ValueError) as exc:
            log.error("[IGN] Download attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)

    log.error("[IGN] All download attempts failed.")
    return []
