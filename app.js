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
   * Returns the circle radius (px) scaled to magnitude.
   * @param {number} mag
   * @returns {number}
   */
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
      maxClusterRadius: 50,
      disableClusteringAtZoom: 13,
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
  function buildPopupHTML(props, coords) {
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

    features.forEach((feature) => {
      const coords = feature.geometry?.coordinates;
      if (!Array.isArray(coords) || coords.length < 2) return;

      const [lon, lat] = coords;
      const props = feature.properties ?? {};
      const mag   = props.mag ?? 0;

      const marker = L.circleMarker([lat, lon], {
        radius:      getMagnitudeRadius(mag),
        fillColor:   getMagnitudeColor(mag),
        color:       'rgba(255,255,255,0.6)',
        weight:      1,
        opacity:     1,
        fillOpacity: 0.8,
      });

      // Popup (click)
      marker.bindPopup(buildPopupHTML(props, coords), { maxWidth: 300 });

      // Tooltip (hover / keyboard focus) — accessible text alternative
      marker.bindTooltip(
        `M${mag != null ? mag.toFixed(1) : '—'} · ${props.place ?? ''}`,
        { direction: 'top', opacity: 0.95 }
      );

      markerLayer.addLayer(marker);
    });
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
      return;
    }

    const maxMag = Math.max(...filtered.map((f) => f.properties?.mag ?? -Infinity));
    setEl('stat-max-mag', isFinite(maxMag) ? maxMag.toFixed(1) : '—');

    const latestMs = Math.max(...filtered.map((f) => f.properties?.time ?? 0));
    setEl('stat-latest', formatDate(latestMs));
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
      renderMarkers(filtered);
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
  }

  // -------------------------------------------------------------------------
  // Public init
  // -------------------------------------------------------------------------

  /**
   * Bootstraps the application. Called once on DOMContentLoaded.
   */
  function init() {
    initMap();
    bindFilterControls();
    refresh();
    refreshTimer = setInterval(refresh, CONFIG.refreshIntervalMs);
  }

  // Expose only what is needed externally
  return { init };
})();

// Boot the app once the DOM is ready
document.addEventListener('DOMContentLoaded', SismocanApp.init);
