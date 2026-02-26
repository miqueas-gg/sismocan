/**
 * app.js — sismocan
 *
 * Responsibilities:
 *  - Initialise the Leaflet map centred on the Canary Islands.
 *  - Fetch data/sismos.json and render seismic events as circle markers.
 *  - Apply user-controlled filters (time period, minimum magnitude).
 *  - Auto-refresh the data every 60 seconds without a full page reload.
 *  - Update the status bar and statistics panel on every cycle.
 *
 * Architecture: IIFE module pattern — all state is encapsulated, only the
 * `init` function is exposed as a public API.
 */

'use strict';

const SismocanApp = (() => {
  // -------------------------------------------------------------------------
  // Configuration (single source of truth)
  // -------------------------------------------------------------------------

  const CONFIG = Object.freeze({
    /** Path to the GeoJSON data file (relative to the page). */
    dataUrl: 'data/sismos.json',

    /** How often (ms) the frontend re-fetches the data file. */
    refreshIntervalMs: 60_000, // 1 minute

    /** Initial map view — centre of the Canary Islands archipelago. */
    mapCenter: [28.3, -15.5],
    mapZoom: 8,

    /** Tile layer configuration. */
    tile: {
      url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
      maxZoom: 19,
    },

    /** Default filter values. */
    defaults: {
      days: '7',
      minMag: 0,
    },
  });

  // -------------------------------------------------------------------------
  // Module state
  // -------------------------------------------------------------------------

  /** @type {L.Map|null} */
  let map = null;

  /** @type {L.LayerGroup|null} */
  let markerLayer = null;

  /** @type {Array<Object>} All GeoJSON features loaded from the data file. */
  let allFeatures = [];

  /** @type {number|null} setInterval handle for auto-refresh. */
  let refreshTimer = null;

  /** @type {Map<string, L.CircleMarker>} Feature ID -> marker, para flyTo. */
  let markerMap = new Map();

  /** @type {L.HeatLayer|null} Capa de calor de Leaflet.heat. */
  let heatLayer = null;

  /** @type {boolean} Si el modo heatmap está activo. */
  let isHeatmapMode = false;

  /** @type {Object|null} Instancia del gráfico de profundidad (sección cruzada). */
  let depthChart = null;

  /** @type {boolean} Si el panel de profundidad está abierto. */
  let isDepthViewOpen = false;

  /** @type {Object|null} Feature más reciente del filtro activo. */
  let latestFeature = null;

  /** Estado del timeline animado. */
  const timeline = {
    running: false,
    index: 0,
    features: [],
    timer: null,
  };

  // -------------------------------------------------------------------------
  // Magnitude helpers
  // -------------------------------------------------------------------------

  /**
   * Returns the fill colour for a marker based on event magnitude.
   * Colours are chosen to be distinguishable for common colour-blindness types.
   * @param {number} mag
   * @returns {string} CSS colour string
   */
  function getMagnitudeColor(mag) {
    if (mag >= 6.0) return '#f87171'; // strong   — red
    if (mag >= 5.0) return '#fb923c'; // moderate — orange
    if (mag >= 4.0) return '#fbbf24'; // light    — amber
    if (mag >= 2.0) return '#34d399'; // minor    — emerald
    return '#818cf8';                  // micro    — indigo
  }

  /**
   * Devuelve el grosor del borde del marcador segun la profundidad.
   * Superficial: borde grueso/brillante. Profundo: borde fino/tenue.
   * @param {number|null} depth  km
   * @returns {{ weight: number, opacity: number }}
   */
  function getDepthBorderStyle(depth) {
    if (depth == null)       return { weight: 1,   opacity: 0.6 };
    if (depth < 30)          return { weight: 3,   opacity: 1.0 };  // superficial
    if (depth < 100)         return { weight: 1.5, opacity: 0.7 };  // intermedio
    return                          { weight: 0.5, opacity: 0.3 };  // profundo
  }
  function getMagnitudeRadius(mag) {
    if (mag >= 6.0) return 20;
    if (mag >= 5.0) return 15;
    if (mag >= 4.0) return 11;
    if (mag >= 2.0) return 7;
    return 5;
  }

  /**
   * Returns a human-readable magnitude class label (in Spanish).
   * @param {number} mag
   * @returns {string}
   */
  function getMagnitudeLabel(mag) {
    if (mag >= 6.0) return 'Fuerte';
    if (mag >= 5.0) return 'Moderado';
    if (mag >= 4.0) return 'Ligero';
    if (mag >= 2.0) return 'Menor';
    return 'Micro';
  }

  // -------------------------------------------------------------------------
  // Date helpers
  // -------------------------------------------------------------------------

  /**
   * Formats a Unix timestamp (ms) as a locale string in Canary Islands time.
   * @param {number|null} epochMs
   * @returns {string}
   */
  function formatDate(epochMs) {
    if (!epochMs) return '—';
    return new Date(epochMs).toLocaleString('es-ES', {
      timeZone: 'Atlantic/Canary',
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  // -------------------------------------------------------------------------
  // Map initialisation
  // -------------------------------------------------------------------------

  function initMap() {
    map = L.map('map', {
      center: CONFIG.mapCenter,
      zoom: CONFIG.mapZoom,
      zoomControl: true,
      // Keyboard navigation enabled by default in Leaflet
    });

    L.tileLayer(CONFIG.tile.url, {
      attribution: CONFIG.tile.attribution,
      maxZoom: CONFIG.tile.maxZoom,
    }).addTo(map);

    markerLayer = L.markerClusterGroup({
      maxClusterRadius: 25,
      disableClusteringAtZoom: 10,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false,
      chunkedLoading: true,
    }).addTo(map);

    // Force Leaflet to recalculate container dimensions once the DOM has fully
    // painted. Without this, percentage-based flex heights can yield a 0-px
    // container at init time and the map renders blank.
    setTimeout(() => map.invalidateSize(), 100);
  }

  // -------------------------------------------------------------------------
  // Data fetching
  // -------------------------------------------------------------------------

  /**
   * Fetches the GeoJSON file, populates `allFeatures` and returns success flag.
   * Adds a cache-busting query string so browsers don't serve a stale copy.
   * @returns {Promise<boolean>}
   */
  async function fetchData() {
    try {
      const res = await fetch(`${CONFIG.dataUrl}?_=${Date.now()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      const geojson = await res.json();
      allFeatures = Array.isArray(geojson.features) ? geojson.features : [];
      return true;
    } catch (err) {
      console.error('[sismocan] Failed to fetch data:', err);
      setStatusError();
      return false;
    }
  }

  // -------------------------------------------------------------------------
  // Filtering
  // -------------------------------------------------------------------------

  /**
   * Reads the current filter values from the DOM.
   * @returns {{ days: string, minMag: number }}
   */
  function getFilters() {
    return {
      days: document.getElementById('filter-days').value,
      minMag: parseFloat(document.getElementById('filter-mag').value),
    };
  }

  /**
   * Applies time and magnitude filters to the full feature list.
   * @param {Array<Object>} features
   * @param {{ days: string, minMag: number }} filters
   * @returns {Array<Object>}
   */
  function applyFilters(features, { days, minMag }) {
    const now = Date.now();
    const cutoffMs = days === 'all' ? 0 : now - Number(days) * 86_400_000;

    return features.filter((f) => {
      const time = f.properties?.time ?? 0;
      const mag  = f.properties?.mag  ?? -Infinity;
      return time >= cutoffMs && mag >= minMag;
    });
  }

  // -------------------------------------------------------------------------
  // Popup content
  // -------------------------------------------------------------------------

  /**
   * Builds accessible HTML popup content for a seismic event.
   * @param {Object} props  GeoJSON feature properties
   * @param {Array}  coords [lon, lat, depth]
   * @returns {string} HTML string
   */
  function buildPopupHTML(props, coords, featureId) {
    const mag    = props.mag   != null ? props.mag.toFixed(1) : '—';
    const depth  = coords[2]   != null ? `${coords[2]} km`   : '—';
    const place  = props.place || 'Desconocida';
    const time   = formatDate(props.time);
    const type   = getMagnitudeLabel(props.mag ?? 0);
    const isIgn  = props.source === 'ign';
    const srcLabel = isIgn ? 'IGN' : 'USGS';

    const detailLink = props.url
      ? `<a href="${props.url}" class="popup-link" target="_blank" rel="noopener noreferrer">
           Ver detalle en ${srcLabel} ↗
         </a>`
      : '';

    const shareBtn = featureId
      ? `<button class="popup-link btn-share" data-feature-id="${featureId}" type="button" style="background:none;border:none;cursor:pointer;padding:0;margin-top:4px;display:block;">
           📎 Copiar enlace
         </button>`
      : '';

    return `
      <div class="quake-popup">
        <div class="popup-header">
          <h3 class="popup-title">Seísmo M${mag}</h3>
          <span class="popup-source popup-source--${isIgn ? 'ign' : 'usgs'}">${srcLabel}</span>
        </div>
        <dl class="popup-data">
          <div class="popup-row">
            <dt>Magnitud</dt>
            <dd><strong>${mag}</strong> <span class="popup-type">(${type})</span></dd>
          </div>
          <div class="popup-row">
            <dt>Profundidad</dt>
            <dd>${depth}</dd>
          </div>
          <div class="popup-row">
            <dt>Fecha/Hora (Canarias)</dt>
            <dd>${time}</dd>
          </div>
          <div class="popup-row popup-place">
            <dt>Localización</dt>
            <dd>${place}</dd>
          </div>
        </dl>
        ${detailLink}
        ${shareBtn}
      </div>`;
  }

  // -------------------------------------------------------------------------
  // Marker rendering
  // -------------------------------------------------------------------------

  /**
   * Clears the layer group and draws one circle marker per filtered feature.
   * @param {Array<Object>} features  Filtered GeoJSON features
   */
  function renderMarkers(features) {
    markerLayer.clearLayers();
    markerMap.clear();

    features.forEach((feature) => {
      const coords = feature.geometry?.coordinates;
      if (!Array.isArray(coords) || coords.length < 2) return;

      const [lon, lat] = coords;
      const props = feature.properties ?? {};
      const mag   = props.mag ?? 0;
      const depth = coords[2] ?? props.depth ?? null;
      const borderStyle = getDepthBorderStyle(depth);

      const marker = L.circleMarker([lat, lon], {
        radius:      getMagnitudeRadius(mag),
        fillColor:   getMagnitudeColor(mag),
        color:       'rgba(255,255,255,0.6)',
        weight:      borderStyle.weight,
        opacity:     borderStyle.opacity,
        fillOpacity: 0.8,
      });

      const fid = feature.id || feature.properties?.id;
      marker.bindPopup(buildPopupHTML(props, coords, fid), { maxWidth: 300 });
      marker.bindTooltip(
        [
          `<strong>M${mag != null ? mag.toFixed(1) : '—'}</strong>`,
          props.place ?? '',
          depth != null ? `${depth} km prof.` : '',
          props.time ? formatDate(props.time) : '',
        ].filter(Boolean).join('<br>'),
        { direction: 'top', opacity: 0.97, className: 'leaflet-tooltip-rich' }
      );

      if (fid) markerMap.set(String(fid), marker);
      markerLayer.addLayer(marker);
    });

    // Actualizar el contador overlay del mapa
    const counter = document.getElementById('map-counter');
    if (counter) {
      counter.textContent = features.length > 0
        ? `${features.length.toLocaleString('es-ES')} seísmos`
        : 'Ningún seísmo con estos filtros';
    }
  }

  // -------------------------------------------------------------------------
  // Stats panel
  // -------------------------------------------------------------------------

  /**
   * Updates the statistics section in the sidebar.
   * @param {Array<Object>} filtered  Currently visible features
   */
  function updateStats(filtered) {
    const setEl = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    setEl('stat-shown', filtered.length.toLocaleString('es-ES'));
    setEl('stat-total', allFeatures.length.toLocaleString('es-ES'));

    if (filtered.length === 0) {
      setEl('stat-max-mag', '—');
      setEl('stat-latest',  '—');
      setEl('stat-latest-place', '—');
      setEl('stat-latest-depth', '—');
      updateLatestEventCard(null);
      updateComparativa(null);
      return;
    }

    const maxMag = Math.max(...filtered.map((f) => f.properties?.mag ?? -Infinity));
    setEl('stat-max-mag', isFinite(maxMag) ? maxMag.toFixed(1) : '—');

    latestFeature = filtered.reduce((best, f) =>
      (f.properties?.time ?? 0) > (best.properties?.time ?? 0) ? f : best
    , filtered[0]);
    const latestMs = latestFeature.properties?.time ?? 0;
    setEl('stat-latest', formatDate(latestMs));
    setEl('stat-latest-place', latestFeature.properties?.place || '—');
    const latestDepth = latestFeature.geometry?.coordinates?.[2];
    setEl('stat-latest-depth', latestDepth != null ? `${latestDepth} km` : '—');

    updateLatestEventCard(latestFeature);
    updateComparativa(filtered);
    if (isDepthViewOpen) updateDepthChart(filtered);
  }

  // -------------------------------------------------------------------------
  // Status bar
  // -------------------------------------------------------------------------

  function setStatusOk() {
    const badge = document.getElementById('status-badge');
    const time  = document.getElementById('status-time');
    if (badge) {
      badge.textContent = `${allFeatures.length.toLocaleString('es-ES')} eventos`;
      badge.className = 'status-badge is-ok';
    }
    if (time) {
      time.textContent = `Últ. actualización: ${new Date().toLocaleTimeString('es-ES')}`;
    }
  }

  function setStatusError() {
    const badge = document.getElementById('status-badge');
    if (badge) {
      badge.textContent = 'Error al cargar datos';
      badge.className = 'status-badge is-error';
    }
  }

  // -------------------------------------------------------------------------
  // Full refresh cycle
  // -------------------------------------------------------------------------

  /**
   * Fetches fresh data, applies current filters and repaints the map.
   * Called on init and every CONFIG.refreshIntervalMs milliseconds.
   */
  async function refresh() {
    const ok = await fetchData();
    if (!ok) return;

    const filters  = getFilters();
    const filtered = applyFilters(allFeatures, filters);

    renderMarkers(filtered);
    updateStats(filtered);
    setStatusOk();
  }

  // -------------------------------------------------------------------------
  // Latest event card
  // -------------------------------------------------------------------------

  /**
   * Actualiza la tarjeta del último seísmo en el sidebar.
   * @param {Object|null} feature  GeoJSON feature más reciente, o null si no hay datos.
   */
  function updateLatestEventCard(feature) {
    const setEl = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    if (!feature) {
      setEl('latest-card-mag',   '—');
      setEl('latest-card-time',  'Sin datos');
      setEl('latest-card-place', '—');
      setEl('latest-card-depth', '—');
      return;
    }

    const props = feature.properties ?? {};
    const coords = feature.geometry?.coordinates ?? [];
    const mag   = props.mag   != null ? `M${props.mag.toFixed(1)}` : '—';
    const depth = coords[2]   != null ? `${coords[2]} km prof.`    : '—';
    const place = props.place || 'Desconocida';
    const timeAgo = formatTimeAgo(props.time);

    setEl('latest-card-mag',   mag);
    setEl('latest-card-time',  timeAgo);
    setEl('latest-card-place', place);
    setEl('latest-card-depth', depth);
  }

  /**
   * Devuelve una cadena "Hace X min/h/días" a partir de un epoch ms.
   * @param {number|null} epochMs
   * @returns {string}
   */
  function formatTimeAgo(epochMs) {
    if (!epochMs) return '—';
    const diff = Date.now() - epochMs;
    const mins = Math.floor(diff / 60_000);
    if (mins < 1)   return 'Ahora mismo';
    if (mins < 60)  return `Hace ${mins} min`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `Hace ${hours} h`;
    const days = Math.floor(hours / 24);
    return `Hace ${days} día${days !== 1 ? 's' : ''}`;
  }

  /**
   * Vuela al mapa y abre el popup de la feature con el ID dado.
   * @param {string} featureId
   */
  function flyToFeature(featureId) {
    const marker = markerMap.get(String(featureId));
    if (!marker) return;
    markerLayer.zoomToShowLayer(marker, () => marker.openPopup());
  }

  // -------------------------------------------------------------------------
  // Comparativa de períodos
  // -------------------------------------------------------------------------

  /**
   * Compara el número de eventos del período actual vs el período anterior del mismo tamaño.
   * @param {Array<Object>|null} filtered  Features del período activo
   */
  function updateComparativa(filtered) {
    const el = document.getElementById('stat-comparativa');
    if (!el) return;

    const filters  = getFilters();
    const days     = filters.days;
    if (days === 'all' || !filtered) {
      el.textContent = '—';
      el.className = '';
      return;
    }

    const daysNum  = Number(days);
    const now      = Date.now();
    const cutoff1  = now - daysNum * 86_400_000;
    const cutoff2  = cutoff1 - daysNum * 86_400_000;
    const minMag   = filters.minMag;

    const prevCount = allFeatures.filter((f) => {
      const t   = f.properties?.time ?? 0;
      const mag = f.properties?.mag  ?? -Infinity;
      return t >= cutoff2 && t < cutoff1 && mag >= minMag;
    }).length;

    const currCount = filtered.length;
    const diff      = currCount - prevCount;
    const pct       = prevCount > 0
      ? Math.abs(Math.round((diff / prevCount) * 100))
      : null;

    if (diff === 0 || prevCount === 0) {
      el.textContent = '= sin cambios';
      el.className = 'comparativa-neutral';
    } else {
      const arrow = diff > 0 ? '▲' : '▼';
      const pctStr = pct != null ? ` (${pct}%)` : '';
      el.textContent = `${arrow} ${Math.abs(diff)} eventos${pctStr}`;
      el.className = diff > 0 ? 'comparativa-up' : 'comparativa-down';
    }
  }

  // -------------------------------------------------------------------------
  // Depth cross-section
  // -------------------------------------------------------------------------

  function toggleDepthView(features) {
    const panel  = document.getElementById('depth-panel');
    const btn    = document.getElementById('btn-depth-view');
    isDepthViewOpen = !isDepthViewOpen;

    if (isDepthViewOpen) {
      panel?.classList.add('is-visible');
      btn?.setAttribute('aria-pressed', 'true');
      updateDepthChart(features);
    } else {
      panel?.classList.remove('is-visible');
      btn?.setAttribute('aria-pressed', 'false');
    }
  }

  /**
   * Diagrama de dispersión: tiempo en X, profundidad (invertida) en Y.
   * El color representa la magnitud.
   * @param {Array<Object>} features
   */
  function updateDepthChart(features) {
    const ctx = document.getElementById('chart-depth');
    if (!ctx || typeof Chart === 'undefined') return;

    // Ordenar por tiempo, tomar los últimos 500 eventos para no saturar
    const sorted = [...features]
      .filter((f) => f.geometry?.coordinates?.[2] != null && f.properties?.time)
      .sort((a, b) => (a.properties.time ?? 0) - (b.properties.time ?? 0))
      .slice(-500);

    const points = sorted.map((f) => ({
      x: f.properties.time,
      y: f.geometry.coordinates[2],
      mag: f.properties.mag ?? 0,
    }));

    const data = {
      datasets: [{
        data:            points,
        parsing:         { xAxisKey: 'x', yAxisKey: 'y' },
        pointBackgroundColor: points.map((p) => getMagnitudeColor(p.mag)),
        pointRadius:     points.map((p) => getMagnitudeRadius(p.mag) * 0.7),
        pointBorderWidth: 0,
      }],
    };

    if (depthChart) {
      depthChart.data = data;
      depthChart.update('none');
      return;
    }

    depthChart = new Chart(ctx, {
      type: 'scatter',
      data,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) => {
                const p = points[item.dataIndex];
                return `M${p.mag.toFixed(1)} · ${p.y} km · ${formatDate(p.x)}`;
              },
            },
            backgroundColor: '#1e293b',
            titleColor: '#f1f5f9',
            bodyColor:  '#94a3b8',
            borderColor: '#334155',
            borderWidth: 1,
          },
        },
        scales: {
          x: {
            type: 'linear',
            display: false,
          },
          y: {
            display: true,
            reverse: true,   // profundidad crece hacia abajo
            grid: { color: 'rgba(255,255,255,0.06)' },
            ticks: {
              color: '#94a3b8',
              font: { size: 10 },
              callback: (v) => `${v} km`,
              maxTicksLimit: 5,
            },
            title: {
              display: true,
              text: 'Profundidad (km)',
              color: '#94a3b8',
              font: { size: 10 },
            },
          },
        },
      },
    });
  }

  // -------------------------------------------------------------------------
  // Theme toggle
  // -------------------------------------------------------------------------

  function initTheme() {
    const saved = localStorage.getItem('sismocan-theme');
    if (saved === 'light') applyTheme('light');
    const btn = document.getElementById('btn-theme');
    if (btn) btn.addEventListener('click', toggleTheme);
  }

  function toggleTheme() {
    const isLight = document.documentElement.classList.contains('theme-light');
    applyTheme(isLight ? 'dark' : 'light');
  }

  function applyTheme(theme) {
    const html = document.documentElement;
    const btn  = document.getElementById('btn-theme');
    if (theme === 'light') {
      html.classList.add('theme-light');
      if (btn) btn.textContent = '🌙';
      localStorage.setItem('sismocan-theme', 'light');
    } else {
      html.classList.remove('theme-light');
      if (btn) btn.textContent = '☀️';
      localStorage.setItem('sismocan-theme', 'dark');
    }
  }

  // -------------------------------------------------------------------------
  // Heatmap
  // -------------------------------------------------------------------------

  /**
   * Alterna entre modo marcador y modo mapa de calor.
   * @param {Array<Object>} features  Features filtradas actualmente
   */
  function toggleHeatmap(features) {
    const btn = document.getElementById('btn-heatmap');
    isHeatmapMode = !isHeatmapMode;

    if (isHeatmapMode) {
      // Ocultar clústeres y mostrar heatmap
      if (map.hasLayer(markerLayer)) map.removeLayer(markerLayer);

      const points = features
        .filter((f) => f.geometry?.coordinates?.length >= 2)
        .map((f) => {
          const [lon, lat] = f.geometry.coordinates;
          const intensity = Math.min((f.properties?.mag ?? 1) / 6, 1);
          return [lat, lon, intensity];
        });

      heatLayer = L.heatLayer(points, {
        radius: 20,
        blur:   18,
        maxZoom: 12,
        gradient: { 0.2: '#818cf8', 0.5: '#34d399', 0.7: '#fbbf24', 0.9: '#fb923c', 1.0: '#f87171' },
      }).addTo(map);

      if (btn) { btn.setAttribute('aria-pressed', 'true'); btn.textContent = '📍 Marcadores'; }
    } else {
      // Volver a marcadores
      if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
      if (!map.hasLayer(markerLayer)) map.addLayer(markerLayer);

      if (btn) { btn.setAttribute('aria-pressed', 'false'); btn.textContent = '🌡️ Mapa de calor'; }
    }
  }

  // -------------------------------------------------------------------------
  // URL sharing
  // -------------------------------------------------------------------------

  /**
   * Maneja clics en los botones "Copiar enlace" dentro de popups.
   * Usa delegación de eventos en el documento.
   */
  function initShareButtons() {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-share');
      if (!btn) return;
      const fid = btn.dataset.featureId;
      if (!fid) return;
      const url = `${location.origin}${location.pathname}?id=${encodeURIComponent(fid)}`;
      navigator.clipboard.writeText(url).then(() => {
        const prev = btn.textContent;
        btn.textContent = '✅ Enlace copiado';
        setTimeout(() => { btn.textContent = prev; }, 2000);
      }).catch(() => {
        prompt('Copia este enlace:', url);
      });
    });
  }

  /**
   * Si la URL tiene ?id=, vuela a esa feature y abre su popup.
   */
  function checkUrlParam() {
    const params = new URLSearchParams(location.search);
    const fid = params.get('id');
    if (!fid) return;
    // Esperar a que los marcadores estén renderizados
    setTimeout(() => flyToFeature(fid), 600);
  }

  // -------------------------------------------------------------------------
  // Timeline animado
  // -------------------------------------------------------------------------

  function openTimeline() {
    const controls = document.getElementById('timeline-controls');
    if (controls) controls.classList.add('is-visible');
    const openBtn = document.getElementById('btn-timeline-open');
    if (openBtn) openBtn.setAttribute('aria-pressed', 'true');

    // Preparar: ordenar todos los features visibles cronológicamente
    const filters  = getFilters();
    const filtered = applyFilters(allFeatures, filters);
    timeline.features = [...filtered].sort((a, b) =>
      (a.properties?.time ?? 0) - (b.properties?.time ?? 0)
    );
    timeline.index   = 0;
    timeline.running = false;

    markerLayer.clearLayers();
    markerMap.clear();
    updateTimelineUI();
  }

  function closeTimeline() {
    stopTimeline();
    const controls = document.getElementById('timeline-controls');
    if (controls) controls.classList.remove('is-visible');
    const openBtn = document.getElementById('btn-timeline-open');
    if (openBtn) openBtn.setAttribute('aria-pressed', 'false');

    // Restaurar vista normal
    const filters  = getFilters();
    const filtered = applyFilters(allFeatures, filters);
    renderMarkers(filtered);
    updateStats(filtered);
  }

  function playTimeline() {
    if (timeline.index >= timeline.features.length) {
      timeline.index = 0;
      markerLayer.clearLayers();
      markerMap.clear();
    }
    timeline.running = true;
    const playBtn = document.getElementById('btn-timeline-play');
    if (playBtn) playBtn.textContent = '⏸';
    stepTimeline();
  }

  function pauseTimeline() {
    timeline.running = false;
    if (timeline.timer) { clearTimeout(timeline.timer); timeline.timer = null; }
    const playBtn = document.getElementById('btn-timeline-play');
    if (playBtn) playBtn.textContent = '▶';
  }

  function stopTimeline() {
    pauseTimeline();
    timeline.index = 0;
  }

  function stepTimeline() {
    if (!timeline.running || timeline.index >= timeline.features.length) {
      pauseTimeline();
      return;
    }

    const feature = timeline.features[timeline.index];
    const coords  = feature.geometry?.coordinates;
    if (Array.isArray(coords) && coords.length >= 2) {
      const [lon, lat] = coords;
      const props = feature.properties ?? {};
      const mag   = props.mag ?? 0;
      const depth = coords[2] ?? props.depth ?? null;
      const borderStyle = getDepthBorderStyle(depth);

      const marker = L.circleMarker([lat, lon], {
        radius:      getMagnitudeRadius(mag),
        fillColor:   getMagnitudeColor(mag),
        color:       'rgba(255,255,255,0.6)',
        weight:      borderStyle.weight,
        opacity:     borderStyle.opacity,
        fillOpacity: 0.85,
      });

      const fid = feature.id || props.id;
      marker.bindPopup(buildPopupHTML(props, coords, fid), { maxWidth: 300 });
      marker.bindTooltip(
        [
          `<strong>M${mag != null ? mag.toFixed(1) : '—'}</strong>`,
          props.place ?? '',
          depth != null ? `${depth} km prof.` : '',
        ].filter(Boolean).join('<br>'),
        { direction: 'top', opacity: 0.97, className: 'leaflet-tooltip-rich' }
      );
      if (fid) markerMap.set(String(fid), marker);
      markerLayer.addLayer(marker);
    }

    timeline.index++;
    updateTimelineUI();

    const speed = parseInt(
      document.getElementById('timeline-speed')?.value ?? '200', 10
    );
    timeline.timer = setTimeout(stepTimeline, speed);
  }

  function updateTimelineUI() {
    const dateEl = document.getElementById('timeline-date');
    const progEl = document.getElementById('timeline-progress');
    const f = timeline.features[timeline.index - 1] ?? timeline.features[0];
    if (dateEl && f) {
      dateEl.textContent = formatDate(f.properties?.time);
    }
    if (progEl) {
      progEl.textContent = `${timeline.index} / ${timeline.features.length}`;
    }
  }

  // -------------------------------------------------------------------------
  // Filter event listeners
  // -------------------------------------------------------------------------

  /**
   * Wires up all filter controls so any change immediately repaints markers
   * without re-fetching data from the network.
   */
  function bindFilterControls() {
    const daysSelect = document.getElementById('filter-days');
    const magRange   = document.getElementById('filter-mag');
    const magNumber  = document.getElementById('filter-mag-number');
    const resetBtn   = document.getElementById('btn-reset');

    function syncAriaRange(val) {
      if (!magRange) return;
      magRange.setAttribute('aria-valuenow',  val);
      magRange.setAttribute('aria-valuetext', `Magnitud mínima ${parseFloat(val).toFixed(1)}`);
    }

    /** Repaints con los filtros actuales (sin petición de red). */
    function onFilterChange() {
      const filters = getFilters();
      syncAriaRange(filters.minMag);
      const filtered = applyFilters(allFeatures, filters);

      if (isHeatmapMode) {
        // Reconstruir el heatmap con los datos filtrados
        if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
        isHeatmapMode = false;  // resetear para que toggleHeatmap lo active
        toggleHeatmap(filtered);
      } else {
        renderMarkers(filtered);
      }
      updateStats(filtered);
    }

    // Slider → sincroniza número
    function onRangeInput() {
      if (magNumber) magNumber.value = parseFloat(magRange.value).toFixed(1);
      onFilterChange();
    }

    // Número → sincroniza slider (y clampea al rango)
    function onNumberInput() {
      let val = parseFloat(magNumber.value);
      if (isNaN(val)) val = 0;
      val = Math.min(6, Math.max(0, val));
      magNumber.value = val.toFixed(1);
      if (magRange) magRange.value = val;
      onFilterChange();
    }

    if (daysSelect) daysSelect.addEventListener('change', onFilterChange);
    if (magRange)   magRange.addEventListener('input',   onRangeInput);
    if (magNumber) {
      magNumber.addEventListener('input',  onNumberInput);
      magNumber.addEventListener('change', onNumberInput);
    }

    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        if (daysSelect) daysSelect.value  = CONFIG.defaults.days;
        if (magRange)   magRange.value    = CONFIG.defaults.minMag;
        if (magNumber)  magNumber.value   = parseFloat(CONFIG.defaults.minMag).toFixed(1);
        onFilterChange();
      });
    }

    // Heatmap toggle
    const heatBtn = document.getElementById('btn-heatmap');
    if (heatBtn) {
      heatBtn.addEventListener('click', () => {
        const filters  = getFilters();
        const filtered = applyFilters(allFeatures, filters);
        toggleHeatmap(filtered);
      });
    }

    // Depth cross-section toggle
    const depthViewBtn = document.getElementById('btn-depth-view');
    if (depthViewBtn) {
      depthViewBtn.addEventListener('click', () => {
        const filters  = getFilters();
        const filtered = applyFilters(allFeatures, filters);
        toggleDepthView(filtered);
      });
    }

    // Depth panel close button
    const depthCloseBtn = document.getElementById('btn-depth-close');
    if (depthCloseBtn) {
      depthCloseBtn.addEventListener('click', () => {
        const filters  = getFilters();
        const filtered = applyFilters(allFeatures, filters);
        if (isDepthViewOpen) toggleDepthView(filtered);
      });
    }

    // Timeline open
    const timelineOpenBtn = document.getElementById('btn-timeline-open');
    if (timelineOpenBtn) {
      timelineOpenBtn.addEventListener('click', openTimeline);
    }

    // Timeline play/pause
    const playBtn = document.getElementById('btn-timeline-play');
    if (playBtn) {
      playBtn.addEventListener('click', () => {
        if (timeline.running) pauseTimeline(); else playTimeline();
      });
    }

    // Timeline close
    const closeBtn = document.getElementById('btn-timeline-close');
    if (closeBtn) closeBtn.addEventListener('click', closeTimeline);
  }

  // -------------------------------------------------------------------------
  // Public init
  // -------------------------------------------------------------------------

  /**
   * Bootstraps the application. Called once on DOMContentLoaded.
   */
  function init() {
    initTheme();
    initMap();
    bindFilterControls();
    initShareButtons();
    refresh();
    refreshTimer = setInterval(refresh, CONFIG.refreshIntervalMs);

    // Tarjeta del último seísmo → volar al mapa
    const latestCard = document.getElementById('latest-event-card');
    if (latestCard) {
      latestCard.addEventListener('click', () => {
        if (!latestFeature) return;
        const coords = latestFeature.geometry?.coordinates;
        if (!Array.isArray(coords) || coords.length < 2) return;
        const fid = latestFeature.id || latestFeature.properties?.id;
        latestCard.classList.add('is-active');
        setTimeout(() => latestCard.classList.remove('is-active'), 1500);
        if (fid) flyToFeature(String(fid));
      });
    }

    // Comprobar si la URL tiene ?id= para abrir un seísmo directo
    checkUrlParam();
  }

  // Expose only what is needed externally
  return { init };
})();

// Boot the app once the DOM is ready
document.addEventListener('DOMContentLoaded', SismocanApp.init);
