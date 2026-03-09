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
