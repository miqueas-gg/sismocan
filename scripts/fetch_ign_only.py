"""
fetch_ign_only.py — ingesta histórica completa solo desde IGN.
Uso: python scripts/fetch_ign_only.py

Diseñado para la primera carga desde cero.
"""
from __future__ import annotations
import json, logging, sys
from pathlib import Path
from calendar import monthrange
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources.ign import fetch_ign

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

HISTORY_START = "2015-01-01"
OUTPUT_FILE   = Path(__file__).resolve().parent.parent / "data" / "sismos.json"


def monthly_windows():
    start = datetime(2015, 1, 1, tzinfo=timezone.utc)
    end   = datetime.now(tz=timezone.utc)
    windows = []
    cursor = start
    while cursor <= end:
        last_day = monthrange(cursor.year, cursor.month)[1]
        chunk_end = cursor.replace(day=last_day)
        if chunk_end > end:
            chunk_end = end
        windows.append((cursor.strftime("%d/%m/%Y"), chunk_end.strftime("%d/%m/%Y")))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1, day=1)
    return windows


def main():
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Empezar siempre desde cero
    collection = {"type": "FeatureCollection", "features": []}
    seen_ids: set = set()
    total = 0

    windows = monthly_windows()
    log.info("Ventanas a procesar: %d (desde %s hasta hoy)", len(windows), HISTORY_START)

    for i, (start_dmy, end_dmy) in enumerate(windows, 1):
        log.info("[%d/%d] %s -> %s", i, len(windows), start_dmy, end_dmy)
        features = fetch_ign(start_dmy, end_dmy)
        added = 0
        for f in features:
            fid = f.get("id")
            if fid and fid not in seen_ids:
                collection["features"].append(f)
                seen_ids.add(fid)
                added += 1
        total += added
        log.info("  +%d nuevos | total acumulado: %d", added, total)

        # Guardar progreso cada 10 ventanas por si se interrumpe
        if i % 10 == 0:
            collection["features"].sort(
                key=lambda f: f.get("properties", {}).get("time") or 0
            )
            with OUTPUT_FILE.open("w", encoding="utf-8") as fh:
                json.dump(collection, fh, ensure_ascii=False, separators=(",", ":"))
            log.info("  Progreso guardado (%d features)", total)

    # Guardar resultado final ordenado
    collection["features"].sort(
        key=lambda f: f.get("properties", {}).get("time") or 0
    )
    with OUTPUT_FILE.open("w", encoding="utf-8") as fh:
        json.dump(collection, fh, ensure_ascii=False, separators=(",", ":"))
    log.info("Ingesta completada. Total eventos IGN: %d -> %s", total, OUTPUT_FILE)


if __name__ == "__main__":
    main()
