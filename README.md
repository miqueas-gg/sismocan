# sismocan

Mapa interactivo de sismicidad en las Islas Canarias, actualizado automáticamente cada 5 minutos.

🌐 **[sismocan.github.io](https://sismocan.github.io)** *(próximamente)*

---

## Descripción

**sismocan** es una aplicación web estática, gratuita y de código abierto que permite visualizar en tiempo casi real los seísmos registrados en el archipiélago canario.

Los datos se obtienen automáticamente del catálogo sísmico del **USGS Earthquake Hazards Program** y se almacenan como GeoJSON en este mismo repositorio. El frontend los consume directamente desde GitHub Pages, sin ningún servidor intermedio.

---

## Características

- 🗺️ Mapa interactivo centrado en Canarias (Leaflet.js)
- 📍 Marcador por evento con popup: magnitud, profundidad, fecha/hora y localización
- 🔍 Filtros por rango temporal y magnitud mínima
- 📊 Histórico acumulativo desde 2015 (todas las magnitudes)
- 🔄 Datos actualizados automáticamente cada 5 minutos mediante GitHub Actions
- 🆓 100% gratuito · sin backend · sin base de datos

---

## Fuente de datos

Los datos sísmicos proceden del servicio público:

**USGS Earthquake Hazards Program — FDSN Event Web Service**
- Endpoint: `https://earthquake.usgs.gov/fdsnws/event/1/query`
- Formato: GeoJSON estándar (`FeatureCollection`)
- Cobertura: Islas Canarias (bbox: lat 27–30, lon -19–-13), magnitud ≥ 0.0, desde 2015

> Los datos del USGS son de dominio público. Más información en [earthquake.usgs.gov](https://earthquake.usgs.gov).

---

## Arquitectura

```
[GitHub Actions cron */5 min]
        │
        ▼
scripts/fetch_quakes.py
  → Consulta USGS FDSN API (ventana deslizante)
  → Deduplica por ID de evento
  → Actualiza data/sismos.json (GeoJSON acumulativo)
  → Commit + push solo si hay eventos nuevos
        │
        ▼
[GitHub Pages sirve data/sismos.json]
        │
        ▼
[Browser — refresco cada 1 min]
  → fetch data/sismos.json
  → filtra en JS por fecha y magnitud
  → pinta marcadores en Leaflet
```

---

## Estructura del repositorio

```
sismocan/
├── index.html                        # Interfaz principal
├── styles.css                        # Estilos
├── app.js                            # Lógica de mapa y filtros
├── data/
│   └── sismos.json                   # GeoJSON acumulativo (generado automáticamente)
├── scripts/
│   └── fetch_quakes.py               # Script Python de ingesta de datos
└── .github/
    └── workflows/
        └── update_data.yml           # GitHub Actions: cron */5 min
```

---

## Desarrollo local

### Requisitos

- Python 3.10+

### Instalación

```bash
git clone https://github.com/sismocan/sismocan.git
cd sismocan
pip install -r requirements.txt
```

### Ejecutar ingesta manualmente

```bash
python scripts/fetch_quakes.py
```

### Servir el frontend en local

```bash
python -m http.server 8000
# Abrir http://localhost:8000
```

---

## Contribuir

Las contribuciones son bienvenidas. Por favor, abre un issue antes de proponer cambios importantes.

1. Haz fork del repositorio
2. Crea una rama (`git checkout -b feature/mi-mejora`)
3. Haz commit de tus cambios
4. Abre un Pull Request contra `develop`

---

## Licencia

MIT License — consulta el fichero [LICENSE](LICENSE) para más detalles.

---

## Atribución

> Datos sísmicos: [USGS Earthquake Hazards Program](https://earthquake.usgs.gov) · Visualización: **sismocan**
