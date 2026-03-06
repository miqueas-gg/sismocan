"""
fetch_gps.py — sismocan GPS deformation monitor
================================================
Descarga las series temporales GPS del Nevada Geodetic Laboratory (NGL,
Universidad de Nevada) para las estaciones permanentes IGN en las islas
volcánicas de Canarias y calcula la tendencia de desplazamiento vertical
reciente.

Fuente de datos: http://geodesy.unr.edu/gps_timeseries/tenv3/IGS14/
Formato:        .tenv3  (columnas separadas por espacio; vertical en metros)
Actualizacion:  NGL publica soluciones diarias con ~1–2 días de retardo.

Salida: data/gps.json

Uso:
    python scripts/fetch_gps.py

Frecuencia recomendada: 1 vez al día (ver .github/workflows/update_gps.yml).
"""

import datetime
import json
import pathlib
import sys
import urllib.request

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

NGL_BASE = "https://geodesy.unr.edu/gps_timeseries/tenv3/IGS14/{station}.tenv3"

# Estaciones IGN permanentes en Canarias procesadas por el NGL.
# IDs de 4 caracteres según el catálogo de estaciones NGL.
STATIONS = [
    {"zone": "el-hierro", "id": "FRON", "name": "El Hierro"},
    {"zone": "la-palma",  "id": "LPAL", "name": "La Palma"},
    {"zone": "tenerife",  "id": "IZAN", "name": "Tenerife"},
]

# Ruta de salida relativa a la raíz del repositorio
REPO_ROOT  = pathlib.Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "gps.json"

# Umbrales de alerta (mm/día)
# Valores conservadores: el fondo de ruido GPS es <0.1 mm/d;
# 0.3 mm/d sostenidos ya es inusual; 1.0 mm/d es claramente anómalo.
THRESHOLD_MODERATE = 0.3   # tendencia elevada
THRESHOLD_HIGH     = 1.0   # tendencia muy elevada

# ---------------------------------------------------------------------------
# Descarga y parseo del formato tenv3
# ---------------------------------------------------------------------------

def fetch_tenv3(station_id: str, timeout: int = 120, retries: int = 3) -> str:
    """
    Descarga el archivo tenv3 del NGL para la estación dada.
    Reintenta hasta `retries` veces ante timeout o error de red.
    Lanza la última excepción si todos los intentos fallan.
    """
    import time as _time
    url = NGL_BASE.format(station=station_id)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            print(f"  GET {url} (intento {attempt}/{retries})", flush=True)
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_exc = exc
            print(f"  AVISO: intento {attempt} fallido: {exc}", flush=True)
            if attempt < retries:
                _time.sleep(10)
    raise last_exc


def parse_tenv3(content: str) -> list[dict]:
    """
    Parsea el contenido de un archivo .tenv3 (formato NGL IGS14) y devuelve
    una lista de entradas ordenadas por fecha, cada una con:
        date   : datetime.date  ISO 8601
        up_mm  : float  desplazamiento vertical en mm vs época de referencia
        su_mm  : float  incertidumbre formal (1σ) en mm

    Formato de columnas (0-indexado):
        0  site
        1  YYMMMDD  (ej. 01MAY10)
        2  yyyy.yyyy
        ....
        12 ____up(m)   ← desplazamiento vertical
        16 sig_u(m)    ← incertidumbre vertical

    Ignora líneas de cabecera y valores con incertidumbre > 50 mm (outliers).
    """
    rows = []
    for line in content.splitlines():
        parts = line.split()
        # Cabecera: primera columna es 'site' (texto), ignorar
        if len(parts) < 17 or parts[0] == 'site':
            continue
        try:
            # Fecha en formato YYMMMDD (ej. "01MAY10" → 2001-05-10)
            date = datetime.datetime.strptime(parts[1], "%y%b%d").date()

            up_m  = float(parts[12])  # columna ____up(m)
            su_m  = float(parts[16])  # columna sig_u(m)

            up_mm = up_m * 1000.0
            su_mm = su_m * 1000.0

            if su_mm > 50:    # outlier — rechazar
                continue

            rows.append({"date": date, "up_mm": up_mm, "su_mm": su_mm})
        except (ValueError, IndexError):
            continue

    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------------------
# Cálculo de tendencia (regresión lineal mínimos cuadrados)
# ---------------------------------------------------------------------------

def linear_trend_mm_per_day(rows, lookback_days):
    """
    Calcula la tendencia lineal (mm/día) del desplazamiento vertical en la
    ventana de los últimos `lookback_days` días.
    Devuelve None si hay menos de 5 puntos en la ventana.
    """
    today   = datetime.date.today()
    cutoff  = today - datetime.timedelta(days=lookback_days)
    window  = [r for r in rows if r["date"] >= cutoff]

    if len(window) < 5:
        return None

    xs = [(r["date"] - cutoff).days for r in window]
    ys = [r["up_mm"]               for r in window]
    n  = len(xs)

    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2        for x in xs)

    if den == 0:
        return None

    return num / den   # mm/día


# ---------------------------------------------------------------------------
# Procesado por estación
# ---------------------------------------------------------------------------

def process_station(station: dict) -> dict:
    """
    Descarga, parsea y analiza los datos GPS de una estación.
    Siempre devuelve un dict con al menos {"zone", "id", "status"}.
    """
    base = {"zone": station["zone"], "id": station["id"], "name": station["name"]}

    try:
        raw   = fetch_tenv3(station["id"])
        rows  = parse_tenv3(raw)

        if not rows:
            return {**base, "status": "no_data"}

        trend_30d = linear_trend_mm_per_day(rows, 30)
        trend_90d = linear_trend_mm_per_day(rows, 90)
        last      = rows[-1]

        # Datos desactualizados si el último punto es de hace > 90 días
        data_age_days = (datetime.date.today() - last["date"]).days
        is_stale      = data_age_days > 90

        # Nivel de alerta basado en tendencia de 30 días
        if trend_30d is None:
            alert_level = 0
        elif abs(trend_30d) >= THRESHOLD_HIGH:
            alert_level = 2
        elif abs(trend_30d) >= THRESHOLD_MODERATE:
            alert_level = 1
        else:
            alert_level = 0

        # Dirección de la tendencia (positivo = alzamiento, negativo = subsidencia)
        direction = None
        if trend_30d is not None:
            direction = "up" if trend_30d > 0.5 else ("down" if trend_30d < -0.5 else "stable")

        return {
            **base,
            "status":           "stale" if is_stale else "ok",
            "lastDate":         last["date"].isoformat(),
            "lastUpMm":         round(last["up_mm"], 2),
            "dataAgeDays":      data_age_days,
            "trend30dMmPerDay": round(trend_30d, 4) if trend_30d is not None else None,
            "trend90dMmPerDay": round(trend_90d, 4) if trend_90d is not None else None,
            "direction":        direction,
            "alertLevel":       alert_level,
            "nPoints":          len(rows),
        }

    except Exception as exc:
        print(f"  ERROR {station['id']}: {exc}", file=sys.stderr)
        return {**base, "status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== fetch_gps.py — Nevada Geodetic Lab ===")
    results = []
    for station in STATIONS:
        print(f"\n[{station['id']}] {station['name']}")
        r = process_station(station)
        results.append(r)
        if r["status"] == "ok":
            t = r.get("trend30dMmPerDay")
            t_str = f"{t:+.2f} mm/día" if t is not None else "—"
            print(f"  → tendencia 30d: {t_str}  |  nivel alerta: {r['alertLevel']}  |  n={r['nPoints']}")
        else:
            print(f"  → status: {r['status']}")

    output = {
        "type":    "gps_deformation",
        "updated": datetime.datetime.utcnow().isoformat() + "Z",
        "stations": results,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nGuardado en {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
