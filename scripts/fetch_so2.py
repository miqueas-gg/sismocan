"""
fetch_so2.py — sismocan SO₂ TROPOMI monitor
============================================
Consulta la columna total de SO₂ (Sentinel-5P TROPOMI, L2) sobre las zonas
volcánicas de Canarias usando la Sentinel Hub Statistical API de Copernicus
Data Space (CDSE).  Es síncrona, rápida y no requiere jobs de batch.

Autenticación:
    Variables de entorno (o GitHub Secrets):
        COPERNICUS_CLIENT_ID      — client_id  (empieza por "sh-")
        COPERNICUS_CLIENT_SECRET  — client_secret

    Mismas credenciales del Copernicus Dashboard
    (https://shapps.dataspace.copernicus.eu/dashboard/)

Salida: data/so2.json

Uso local:
    pip install requests
    set COPERNICUS_CLIENT_ID=sh-...
    set COPERNICUS_CLIENT_SECRET=...
    python scripts/fetch_so2.py

Frecuencia recomendada: 1 vez al día (ver .github/workflows/update_so2.yml).
"""

import datetime
import json
import os
import pathlib
import statistics
import sys

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

TOKEN_URL  = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
STATS_URL  = "https://sh.dataspace.copernicus.eu/api/v1/statistics"

# Conversión: 1 DU = 4.4615×10⁻⁴ mol/m²
MOL_TO_DU = 1.0 / 4.4615e-4   # ≈ 2241.7

# Mínimo de días de historial para calcular baseline
MIN_BASELINE_DAYS = 15

# Umbrales absolutos (cuando no hay baseline suficiente)
ABS_THRESH_L1_DU = 10.0   # > 10 DU → alerta 1
ABS_THRESH_L2_DU = 30.0   # > 30 DU → alerta 2

# Factores de anomalía relativa (cuando sí hay baseline)
REL_THRESH_L1 = 3.0        # > 3× baseline → alerta 1
REL_THRESH_L2 = 10.0       # >10× baseline → alerta 2

LOOKBACK_DAYS = 7

ZONES = [
    {
        "id":   "el-hierro",
        "name": "El Hierro",
        "bbox": [-18.25, 27.55, -17.75, 27.90],   # [W, S, E, N]
    },
    {
        "id":   "la-palma",
        "name": "La Palma",
        "bbox": [-17.98, 28.40, -17.70, 28.90],
    },
    {
        "id":   "tenerife",
        "name": "Tenerife · Teide",
        "bbox": [-16.95, 27.90, -16.10, 28.60],
    },
]

OUT_FILE = pathlib.Path(__file__).parent.parent / "data" / "so2.json"

# Evalscript: media de SO₂ mol/m² sobre píxeles válidos por día
EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["SO2", "dataMask"]}],
    output: [
      {id: "so2",      bands: 1, sampleType: "FLOAT32"},
      {id: "dataMask", bands: 1, sampleType: "UINT8"}
    ],
    mosaicking: Mosaicking.ORBIT
  };
}
function evaluatePixel(samples) {
  var total = 0, count = 0;
  for (var i = 0; i < samples.length; i++) {
    var s = samples[i];
    if (s.dataMask === 1 && s.SO2 !== undefined) {
      total += s.SO2;
      count++;
    }
  }
  var mean = count > 0 ? total / count : NaN;
  return {
    so2:      [mean],
    dataMask: [count > 0 ? 1 : 0]
  };
}
"""

# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

def get_token(client_id, client_secret):
    """Obtiene un access token OAuth2 client_credentials para Sentinel Hub."""
    try:
        import requests
    except ImportError:
        print("ERROR: pip install requests", file=sys.stderr)
        sys.exit(1)
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Consulta SO₂ via Sentinel Hub Statistical API
# ---------------------------------------------------------------------------

def query_so2(token, zone, start_date, end_date):
    """
    Consulta la Statistical API y devuelve lista de (date_str, mol_m2) por día.
    """
    try:
        import requests
    except ImportError:
        sys.exit(1)

    w, s, e, n = zone["bbox"]

    payload = {
        "input": {
            "bounds": {
                "bbox": [w, s, e, n],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{"type": "sentinel-5p-l2", "dataFilter": {}}],
        },
        "aggregation": {
            "timeRange": {
                "from": f"{start_date}T00:00:00Z",
                "to":   f"{end_date}T23:59:59Z",
            },
            "aggregationInterval": {"of": "P1D"},
            "evalscript": EVALSCRIPT,
            "resx": 0.05,
            "resy": 0.05,
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    resp = requests.post(STATS_URL, headers=headers, json=payload, timeout=60)

    if resp.status_code != 200:
        print(f"    AVISO HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return []

    results = []
    for interval in resp.json().get("data", []):
        date_str = interval.get("interval", {}).get("from", "")[:10]
        mean_val = (interval.get("outputs", {})
                            .get("so2", {})
                            .get("bands", {})
                            .get("B0", {})
                            .get("stats", {})
                            .get("mean"))
        if mean_val is not None and not (isinstance(mean_val, float) and mean_val != mean_val):
            results.append((date_str, float(mean_val)))

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_alert_level(so2_du, history, baseline):
    if so2_du is None:
        return 0
    if baseline is not None and baseline > 0:
        ratio = so2_du / baseline
        if ratio >= REL_THRESH_L2:  return 2
        if ratio >= REL_THRESH_L1:  return 1
        return 0
    if so2_du >= ABS_THRESH_L2_DU:  return 2
    if so2_du >= ABS_THRESH_L1_DU:  return 1
    return 0


def load_existing():
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "type":    "so2_tropomi",
        "updated": None,
        "zones":   {z["id"]: {"history": []} for z in ZONES},
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    client_id     = os.environ.get("COPERNICUS_CLIENT_ID")
    client_secret = os.environ.get("COPERNICUS_CLIENT_SECRET")

    if not client_id or not client_secret:
        print(
            "ERROR: define COPERNICUS_CLIENT_ID y COPERNICUS_CLIENT_SECRET",
            file=sys.stderr,
        )
        sys.exit(1)

    # ---- Token OAuth2 ------------------------------------------------------
    print("Obteniendo token OAuth2…")
    try:
        token = get_token(client_id, client_secret)
        print("  Token OK")
    except Exception as exc:
        print(f"ERROR de autenticación: {exc}", file=sys.stderr)
        sys.exit(1)

    # ---- Rango temporal ----------------------------------------------------
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end   = today.isoformat()
    print(f"  Ventana: {start} → {end}")

    # ---- Cargar historial --------------------------------------------------
    data = load_existing()

    # ---- Consultar cada zona -----------------------------------------------
    for zone in ZONES:
        zid = zone["id"]
        if zid not in data["zones"]:
            data["zones"][zid] = {"history": []}
        zdata = data["zones"][zid]

        print(f"\n  Zona: {zone['name']} …")

        try:
            daily_vals = query_so2(token, zone, start, end)
        except Exception as exc:
            print(f"    ERROR: {exc}", file=sys.stderr)
            daily_vals = []

        if daily_vals:
            for date_str, mol_m2 in daily_vals:
                so2_du = max(0.0, mol_m2 * MOL_TO_DU)  # clamp negativo = ruido
                if so2_du > 10_000:
                    continue
                zdata["history"] = [h for h in zdata["history"] if h["date"] != date_str]
                zdata["history"].append({"date": date_str, "so2_du": round(so2_du, 3)})
            zdata["history"].sort(key=lambda h: h["date"])
            zdata["history"] = zdata["history"][-180:]
            print(f"    {len(daily_vals)} días recibidos; último: {daily_vals[-1][0]} → {max(0.0, daily_vals[-1][1]*MOL_TO_DU):.3f} DU")
        else:
            print("    Sin datos TROPOMI para esta ventana.")

        # ---- Baseline y anomalía ------------------------------------------
        history_vals = [h["so2_du"] for h in zdata["history"] if h.get("so2_du") is not None]
        baseline     = None
        anomaly      = None

        if len(history_vals) >= MIN_BASELINE_DAYS:
            baseline = round(statistics.mean(history_vals), 3)
            anchor   = zdata["history"][-1]["so2_du"] if history_vals else None
            if baseline and baseline > 0 and anchor is not None:
                anomaly = round(anchor / baseline, 2)

        latest_du = zdata["history"][-1]["so2_du"] if zdata["history"] else None
        alert_lvl = compute_alert_level(latest_du, zdata["history"], baseline)

        zdata["latest_du"]   = latest_du
        zdata["baseline_du"] = baseline
        zdata["anomaly"]     = anomaly
        zdata["alertLevel"]  = alert_lvl
        zdata["nDays"]       = len(history_vals)
        zdata["status"]      = "ok" if latest_du is not None else "pending"

        print(f"    latest={latest_du} DU  baseline={baseline} DU  anomalía=×{anomaly}  level={alert_lvl}")

    # ---- Guardar -----------------------------------------------------------
    data["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"\nGuardado en {OUT_FILE}")


if __name__ == "__main__":
    main()



def compute_alert_level(so2_du, history, baseline):
    """
    Devuelve alertLevel 0-2 basado en umbral absoluto o relativo.
    """
    if so2_du is None or so2_du < 0:
        return 0

    if baseline is not None and baseline > 0:
        ratio = so2_du / baseline
        if ratio >= REL_THRESH_L2:
            return 2
        if ratio >= REL_THRESH_L1:
            return 1
        return 0

    # Sin baseline: umbrales absolutos
    if so2_du >= ABS_THRESH_L2_DU:
        return 2
    if so2_du >= ABS_THRESH_L1_DU:
        return 1
    return 0


def load_existing():
    """Carga el so2.json existente o devuelve una estructura vacía."""
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "type":    "so2_tropomi",
        "updated": None,
        "zones":   {z["id"]: {"history": []} for z in ZONES},
    }


def _extract_value(result):
    """
    Extrae el primer valor numérico de la respuesta de openEO aggregate_spatial.

    openEO devuelve algo como:
        {"2026-02-20T00:00:00Z": [[0.001234]]}   (dict fecha→[[valores]])
    o un objeto xarray, o la respuesta puede tener formato distinto.
    """
    # --- Caso dict de fechas ------------------------------------------------
    if isinstance(result, dict):
        # Tomamos el valor mediando todos los timestamps disponibles
        values = []
        for v in result.values():
            try:
                # v puede ser [[num]] o [num] o num
                if isinstance(v, (list, tuple)):
                    flat = v[0] if isinstance(v[0], (list, tuple)) else v
                    for x in flat:
                        if x is not None and not (isinstance(x, float) and (x != x)):  # not NaN
                            values.append(float(x))
                elif isinstance(v, (int, float)) and not (isinstance(v, float) and v != v):
                    values.append(float(v))
            except Exception:
                continue
        if values:
            return statistics.median(values)
        return None

    # --- Caso xarray DataArray / Dataset ------------------------------------
    try:
        import numpy as np
        arr = result
        # intentar .values o conversión directa
        if hasattr(arr, "values"):
            arr = arr.values
        data = arr.ravel()
        valid = data[~(data != data)]  # quitar NaN; numpy workaround para py3.9
        if len(valid) == 0:
            return None
        return float(np.nanmedian(arr))
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def fetch_so2(conn, zone, start_date, end_date):
    """
    Consulta el SO₂ medio sobre bbox en el intervalo dado.
    Devuelve valor en DU (float) o None si no hay datos.
    """
    try:
        cube = conn.load_collection(
            COLLECTION,
            spatial_extent=zone["bbox"],
            temporal_extent=[start_date, end_date],
            bands=[BAND],
        )

        # aggregate_spatial → media sobre el polígono de la zona
        geom = bbox_geojson(zone["bbox"])
        agg = cube.aggregate_spatial(geometries=geom, reducer="mean")

        # Ejecutar sincrónicamente (devolverá un dict con la serie temporal)
        result = agg.execute()

        val_mol = _extract_value(result)
        if val_mol is None:
            return None

        so2_du = val_mol * MOL_TO_DU
        # Clamp a 0: valores ligeramente negativos son ruido de fondo normal
        so2_du = max(0.0, so2_du)
        # Sanity check: descartar valores físicamente irreales (> 10 000 DU)
        if so2_du > 10_000:
            return None
        return round(so2_du, 3)

    except Exception as exc:
        print(f"      AVISO: error al obtener SO₂ para {zone['name']}: {exc}",
              file=sys.stderr)
        return None


def main():
    # ---- Verificar credenciales --------------------------------------------
    client_id     = os.environ.get("COPERNICUS_CLIENT_ID")
    client_secret = os.environ.get("COPERNICUS_CLIENT_SECRET")

    if not client_id or not client_secret:
        print(
            "ERROR: variables de entorno COPERNICUS_CLIENT_ID y "
            "COPERNICUS_CLIENT_SECRET no definidas.\n"
            "  Créalas en: https://dataspace.copernicus.eu/ → "
            "Mi cuenta → OAuth Clients",
            file=sys.stderr,
        )
        sys.exit(1)

    # ---- Importar openeo (instalarlo si falta) ------------------------------
    try:
        import openeo
    except ImportError:
        print("ERROR: paquete 'openeo' no instalado. Ejecuta: pip install openeo",
              file=sys.stderr)
        sys.exit(1)

    # ---- Conectar y autenticar ---------------------------------------------
    print(f"Conectando a {OPENEO_URL} …")
    conn = openeo.connect(OPENEO_URL)

    try:
        conn.authenticate_oidc_client_credentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        print("  Autenticación OK")
    except Exception as exc:
        print(f"ERROR de autenticación: {exc}", file=sys.stderr)
        sys.exit(1)

    # ---- Verificar que la colección existe ---------------------------------
    try:
        col_ids = [c["id"] for c in conn.list_collections()]
        if COLLECTION not in col_ids:
            # Buscar cualquier S5P disponible como ayuda al diagnóstico
            s5p_cols = [c for c in col_ids if "5P" in c.upper() or "SO2" in c.upper()]
            print(
                f"ERROR: la colección '{COLLECTION}' no existe en este backend.\n"
                f"  Colecciones Sentinel-5P disponibles: {s5p_cols or 'ninguna'}\n"
                f"  Actualiza la variable COLLECTION en este script.",
                file=sys.stderr,
            )
            sys.exit(1)
    except Exception as exc:
        print(f"AVISO: no se pudo listar colecciones: {exc}", file=sys.stderr)

    # ---- Rango temporal (últimos LOOKBACK_DAYS días) -----------------------
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    end   = today.isoformat()
    print(f"  Ventana: {start} → {end}")

    # ---- Cargar historial existente ----------------------------------------
    data = load_existing()

    # ---- Consultar cada zona -----------------------------------------------
    for zone in ZONES:
        zid = zone["id"]
        if zid not in data["zones"]:
            data["zones"][zid] = {"history": []}
        zdata = data["zones"][zid]

        print(f"\n  Zona: {zone['name']} …")
        so2_du = fetch_so2(conn, zone, start, end)

        if so2_du is not None:
            entry_date = today.isoformat()
            # Reemplazar si ya existe entrada de hoy
            zdata["history"] = [h for h in zdata["history"] if h["date"] != entry_date]
            zdata["history"].append({"date": entry_date, "so2_du": so2_du})
            # Mantener máximo 180 días de historial
            zdata["history"].sort(key=lambda h: h["date"])
            zdata["history"] = zdata["history"][-180:]
            print(f"    SO₂ = {so2_du:.2f} DU")
        else:
            print(f"    Sin datos TROPOMI para esta ventana.")

        # ---- Calcular baseline y anomalía ----------------------------------
        history_vals = [h["so2_du"] for h in zdata["history"] if h.get("so2_du") is not None]
        baseline     = None
        anomaly      = None

        if len(history_vals) >= MIN_BASELINE_DAYS:
            baseline = round(statistics.mean(history_vals), 3)
            anchor   = zdata["history"][-1]["so2_du"] if history_vals else None
            if baseline and baseline > 0 and anchor is not None:
                anomaly = round(anchor / baseline, 2)

        latest_du  = zdata["history"][-1]["so2_du"] if zdata["history"] else None
        alert_lvl  = compute_alert_level(latest_du, zdata["history"], baseline)

        zdata["latest_du"]   = latest_du
        zdata["baseline_du"] = baseline
        zdata["anomaly"]     = anomaly
        zdata["alertLevel"]  = alert_lvl
        zdata["nDays"]       = len(history_vals)
        zdata["status"]      = "ok" if latest_du is not None else "pending"

        print(f"    baseline={baseline} DU  anomalía=×{anomaly}  alertLevel={alert_lvl}")

    # ---- Guardar -----------------------------------------------------------
    data["updated"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"\nGuardado en {OUT_FILE}")


if __name__ == "__main__":
    main()
