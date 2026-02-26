"""
import_ign_geojson.py — importa un GeoJSON descargado manualmente del IGN
--------------------------------------------------------------------------
Normaliza los campos IGN al schema unificado de sismocan y escribe el
resultado en data/sismos.json.

Uso:
    python scripts/import_ign_geojson.py <ruta_al_geojson>

Si no se pasa argumento, busca automáticamente el primer .geojson en data/.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

REPO_ROOT   = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "data" / "sismos.json"


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

def _parse_time(fecha: str, hora: str) -> int | None:
    """Convierte 'DD/MM/YYYY' + 'HH:MM:SS' a epoch ms UTC."""
    raw = f"{fecha} {hora}".strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def normalize(feature: dict) -> dict | None:
    """Convierte un feature IGN al schema sismocan."""
    props  = feature.get("properties") or {}
    geom   = feature.get("geometry")   or {}
    coords = geom.get("coordinates")   or []

    if len(coords) < 2:
        return None

    lon = float(coords[0])
    lat = float(coords[1])

    mag   = props.get("magnitud")
    depth = props.get("profundidad")
    place = props.get("localizacion") or ""
    evid  = props.get("evid") or f"{lat}_{lon}"

    epoch_ms = _parse_time(
        props.get("fecha", ""),
        props.get("hora",  ""),
    )

    norm_coords = [lon, lat]
    if depth is not None:
        norm_coords.append(float(depth))

    return {
        "type": "Feature",
        "id":   f"ign_{evid}",
        "properties": {
            "mag":    float(mag)   if mag   is not None else None,
            "depth":  float(depth) if depth is not None else None,
            "place":  place,
            "time":   epoch_ms,
            "url":    None,
            "source": "ign",
        },
        "geometry": {
            "type":        "Point",
            "coordinates": norm_coords,
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Localizar archivo de entrada
    if len(sys.argv) > 1:
        source = Path(sys.argv[1])
    else:
        candidates = sorted((REPO_ROOT / "data").glob("*.geojson"))
        if not candidates:
            log.error("No se encontró ningún .geojson en data/. Pasa la ruta como argumento.")
            sys.exit(1)
        source = candidates[0]
        log.info("Usando archivo: %s", source)

    if not source.exists():
        log.error("Archivo no encontrado: %s", source)
        sys.exit(1)

    log.info("Leyendo %s (%.1f MB)...", source.name, source.stat().st_size / 1_048_576)

    with source.open(encoding="utf-8") as fh:
        raw = json.load(fh)

    raw_features = raw.get("features", [])
    log.info("Features en el archivo fuente: %d", len(raw_features))

    # Normalizar
    normalized = []
    seen_ids: set[str] = set()
    skipped = 0

    for f in raw_features:
        n = normalize(f)
        if n is None:
            skipped += 1
            continue
        fid = n["id"]
        if fid in seen_ids:
            skipped += 1
            continue
        seen_ids.add(fid)
        normalized.append(n)

    log.info("Normalizados: %d | Descartados: %d", len(normalized), skipped)

    # Ordenar cronológicamente
    normalized.sort(key=lambda f: f.get("properties", {}).get("time") or 0)

    collection = {"type": "FeatureCollection", "features": normalized}
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as fh:
        json.dump(collection, fh, ensure_ascii=False, separators=(",", ":"))

    size_kb = OUTPUT_FILE.stat().st_size / 1024
    log.info("Guardado: %s (%.1f KB, %d features)", OUTPUT_FILE, size_kb, len(normalized))


if __name__ == "__main__":
    main()
