"""
fetch_gps.py — sismocan GPS deformation monitor
================================================
Descarga las series temporales GPS de la Red Permanente Europea (EPN) del
EUREF Permanent GNSS Network para las estaciones permanentes IGN en las islas
volcánicas de Canarias y calcula la tendencia de desplazamiento vertical
reciente.

Fuente primaria:  https://epncb.oma.be/pub/station/coord/EPN/Time_Series/
Formato EUREF:    .dat  columnas: GPS_week  N_res  N_sig  E_res  E_sig  U_res  U_sig  (mm)
                  Los valores son residuos sobre una tendencia lineal a largo plazo.
Actualización:    EUREF publica soluciones semanales con soluciones diarias integradas.
Licencia:         CC-BY-4.0

Fuente fallback:  NGL (Nevada Geodetic Lab) para El Hierro (no disponible en EUREF)
URL:              https://geodesy.unr.edu/gps_timeseries/tenv3/IGS14/{station}.tenv3

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

# Fuente primaria: EUREF EPN Time Series
EUREF_BASE = "https://epncb.oma.be/pub/station/coord/EPN/Time_Series/{filename}"

# Fuente fallback: NGL (solo para estaciones no disponibles en EUREF)
NGL_BASE = "https://geodesy.unr.edu/gps_timeseries/tenv3/IGS14/{station}.tenv3"

# Época GPS (origen para convertir GPS week a fecha calendario)
GPS_EPOCH = datetime.date(1980, 1, 6)

# Estaciones: EUREF tiene LPAL y IZAN; El Hierro solo en NGL.
# "euref_file" → nombre del .dat en el directorio Time_Series de EUREF
# "ngl_id"     → ID de 4 caracteres en NGL (solo si no hay EUREF)
STATIONS = [
    {
        "zone":       "el-hierro",
        "id":         "FRON",
        "name":       "El Hierro",
        "source":     "ngl",
        "ngl_id":     "FRON",
    },
    {
        "zone":       "la-palma",
        "id":         "LPAL",
        "name":       "La Palma",
        "source":     "euref",
        "euref_file": "LPAL_81701M001.dat",
    },
    {
        "zone":       "tenerife",
        "id":         "IZAN",
        "name":       "Tenerife",
        "source":     "euref",
        "euref_file": "IZAN_31309M002.dat",
    },
]

# Ruta de salida relativa a la raíz del repositorio
REPO_ROOT   = pathlib.Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "gps.json"

# Umbrales de alerta (mm/día)
THRESHOLD_MODERATE = 0.3   # tendencia elevada
THRESHOLD_HIGH     = 1.0   # tendencia muy elevada

# ---------------------------------------------------------------------------
# Descarga genérica con reintentos
# ---------------------------------------------------------------------------

def fetch_url(url: str, timeout: int = 120, retries: int = 3) -> str:
    """Descarga texto de una URL con reintentos ante fallos de red."""
    import time as _time
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


# ---------------------------------------------------------------------------
# Parseo del formato EUREF .dat
# ---------------------------------------------------------------------------

def gps_week_to_date(gps_week_float: float) -> datetime.date:
    """
    Convierte GPS week (float, p.ej. 2384.286) a fecha calendario.
    El número entero es la semana GPS, la fracción el día de la semana (0=domingo).
    """
    total_days = round(gps_week_float * 7)
    return GPS_EPOCH + datetime.timedelta(days=total_days)


def parse_euref_dat(content: str) -> list[dict]:
    """
    Parsea el contenido de un archivo .dat de la serie temporal EUREF/EPN.

    Formato (README_TS_FORMAT.txt):
        GPS_week  N_res  N_sig  E_res  E_sig  U_res  U_sig   (mm)

    Devuelve lista de dicts con {date, up_mm, su_mm} ordenada por fecha.
    Rechaza filas con sigma_U > 50 mm (outliers / periodos sin solución).
    """
    rows = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            gps_week = float(parts[0])
            # Sanidad: semanas GPS razonables (post-1994 hasta 2050)
            if not (700 <= gps_week <= 3700):
                continue
            date  = gps_week_to_date(gps_week)
            up_mm = float(parts[5])   # U_res en mm
            su_mm = float(parts[6])   # U_sig en mm
            if su_mm > 50:
                continue
            rows.append({"date": date, "up_mm": up_mm, "su_mm": su_mm})
        except (ValueError, IndexError):
            continue

    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------------------
# Parseo del formato NGL .tenv3 (fallback para El Hierro)
# ---------------------------------------------------------------------------

def fetch_tenv3(station_id: str) -> str:
    url = NGL_BASE.format(station=station_id)
    return fetch_url(url)


def parse_tenv3(content: str) -> list[dict]:
    """
    Parsea el contenido de un archivo .tenv3 (formato NGL IGS14).

    Formato de columnas (0-indexado):
        0  site
        1  YYMMMDD  (ej. 01MAY10)
        12 ____up(m)
        16 sig_u(m)
    """
    rows = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 17 or parts[0] == 'site':
            continue
        try:
            date  = datetime.datetime.strptime(parts[1], "%y%b%d").date()
            up_mm = float(parts[12]) * 1000.0
            su_mm = float(parts[16]) * 1000.0
            if su_mm > 50:
                continue
            rows.append({"date": date, "up_mm": up_mm, "su_mm": su_mm})
        except (ValueError, IndexError):
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------------------
# Cálculo de tendencia (regresión lineal mínimos cuadrados)
# ---------------------------------------------------------------------------

def linear_trend_mm_per_day(rows: list, lookback_days: int):
    """
    Calcula la tendencia lineal (mm/día) del desplazamiento vertical en la
    ventana de los últimos `lookback_days` días DESDE EL ÚLTIMO PUNTO DE DATOS.

    Se mide desde el dato más reciente hacia atrás, no desde hoy, para que la
    tendencia sea siempre calculable aunque los datos tengan cierto desfase
    (p.ej., EUREF EPN tiene retraso de ~4-5 meses en su producto final).

    Devuelve None si hay menos de 5 puntos en la ventana.
    """
    if not rows:
        return None
    last_date = rows[-1]["date"]
    cutoff    = last_date - datetime.timedelta(days=lookback_days)
    window    = [r for r in rows if r["date"] >= cutoff]

    if len(window) < 5:
        return None

    xs = [(r["date"] - cutoff).days for r in window]
    ys = [r["up_mm"]               for r in window]
    n  = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2        for x in xs)

    return num / den if den != 0 else None   # mm/día


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
        # --- Descarga y parseo según fuente ---
        if station["source"] == "euref":
            url  = EUREF_BASE.format(filename=station["euref_file"])
            raw  = fetch_url(url)
            rows = parse_euref_dat(raw)
            source_label = f"EUREF EPN ({station['euref_file']})"
        else:
            raw  = fetch_tenv3(station["ngl_id"])
            rows = parse_tenv3(raw)
            source_label = f"NGL ({station['ngl_id']})"

        if not rows:
            return {**base, "status": "no_data", "source": source_label}

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

        # Dirección de la tendencia
        direction = None
        if trend_30d is not None:
            direction = "up" if trend_30d > 0.5 else ("down" if trend_30d < -0.5 else "stable")

        return {
            **base,
            "status":           "stale" if is_stale else "ok",
            "source":           source_label,
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
    print("=== fetch_gps.py — EUREF EPN + NGL fallback ===")
    results = []
    for station in STATIONS:
        print(f"\n[{station['id']}] {station['name']} (fuente: {station['source'].upper()})")
        r = process_station(station)
        results.append(r)
        if r["status"] in ("ok", "stale"):
            t = r.get("trend30dMmPerDay")
            t_str = f"{t:+.2f} mm/día" if t is not None else "—"
            age   = r.get("dataAgeDays", "?")
            print(f"  → tendencia 30d: {t_str}  |  nivel alerta: {r['alertLevel']}  "
                  f"|  edad datos: {age}d  |  n={r['nPoints']}  |  status: {r['status']}")
        else:
            print(f"  → status: {r['status']}")

    output = {
        "type":     "gps_deformation",
        "updated":  datetime.datetime.utcnow().isoformat() + "Z",
        "stations": results,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nGuardado en {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
