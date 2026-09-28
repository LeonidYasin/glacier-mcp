// glacier-mcp frontend: OpenLayers map + GeoTIFF basemap + multi-polygon editing.
//
// Responsibilities:
//   * show a loaded GeoTIFF as a static image layer in its own CRS
//   * let the user draw several glaciers as separate polygons
//   * let the user select, edit and delete any of them
//   * POST every change to the backend so the shared PolygonState matches
//   * listen on /ws for edits coming from the agent and re-render
//   * provide Undo / Redo / Export shapefile buttons
//
// Coordinate handling: the backend stores all polygons in the CRS of the
// currently loaded GeoTIFF. The map view is switched to that CRS on upload,
// so we send and receive coordinates in the same units the backend wants.
// Before any raster is loaded the map falls back to EPSG:3857.

const BACKEND = window.location.origin;

if (typeof proj4 !== "undefined" && ol.proj.proj4) {
  ol.proj.proj4.register(proj4);
} else {
  console.warn("proj4js not available at load time; raster CRS will be unavailable.");
}

// Palette for polygon outlines - cycles as you add more glaciers.
const COLORS = [
  "#00e0ff", // cyan
  "#ffb020", // amber
  "#8dff5a", // lime
  "#ff5a8d", // pink
  "#c58aff", // violet
  "#5a8dff", // blue
];

// ---- WebSocket sync -------------------------------------------------------

let ws = null;

function connectWebSocket() {
  ws = new WebSocket(`ws://${window.location.host}/ws`);
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "polygons") {
      applyCollectionFromServer(msg);
    }
  };
  ws.onclose = () => {
    setTimeout(connectWebSocket, 1000);
  };
}

// ---- Map state ------------------------------------------------------------

let map = null;
let vectorSource = null;
let vectorLayer = null;
let draw = null;
let modify = null;
let rasterLayer = null;
let currentCrsCode = "EPSG:3857";
let selectedIndex = null; // which polygon is currently active for editing
let suppressServerSync = false; // true while applying a server snapshot

// ---- DOM handles ----------------------------------------------------------

const fileInput = document.getElementById("geotiff-file");
const infoSpan = document.getElementById("geotiff-info");
const statusSpan = document.getElementById("status");

function setStatus(text) {
  if (statusSpan) statusSpan.textContent = text;
}

function setInfo(text) {
  if (!infoSpan) return;
  infoSpan.textContent = text;
  infoSpan.title = text;
}

// ---- Feature styling ------------------------------------------------------
//
// Selected polygon: thick white halo + bright fill, so it is unmistakable.
// Unselected: coloured outline + a coloured fill at 20% alpha. The fill
// MUST have a solid (non-white) colour and enough alpha, otherwise
// OpenLayers hit-detection treats interior pixels as transparent and a
// click inside the polygon does not select it (only clicks on the line did).

function _rgba(hex, alpha) {
  // hex like "#00e0ff" -> "rgba(0,224,255,alpha)"
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function styleForFeature(feature) {
  const idx = feature.get("index");
  const isSelected = idx !== null && idx === selectedIndex;
  const color = COLORS[(idx ?? 0) % COLORS.length];

  if (isSelected) {
    return new ol.style.Style({
      stroke: new ol.style.Stroke({ color: "#ffffff", width: 5 }),
      fill: new ol.style.Fill({ color: _rgba(color, 0.45) }),
    });
  }
  return new ol.style.Style({
    stroke: new ol.style.Stroke({ color, width: 2 }),
    fill: new ol.style.Fill({ color: _rgba(color, 0.20) }),
  });
}

// ---- Draw/Select mode -----------------------------------------------------
//
// The classic GIS pattern: Select is the default, Draw is a temporary mode
// you enter to add a new polygon and leave immediately after. Keeping Draw
// always-on made every click start a new polygon — that is the bug the user
// reported ("click selects nothing, starts drawing instead").

let drawMode = false;

function setDrawMode(on) {
  drawMode = on;
  if (draw) draw.setActive(on);
  const btn = document.getElementById("btn-draw");
  if (btn) {
    btn.classList.toggle("btn-active", on);
    btn.textContent = on ? "Drawing... (Esc to cancel)" : "Draw new polygon";
  }
  if (!on && draw) draw.abortDrawing();
  setStatus(
    on
      ? "drawing — click to add vertices, double-click to finish"
      : "select mode — click a polygon to select it"
  );
}

// Local sanity check: a polygon must have >=3 vertices and non-zero area.
// The backend refuses empty/degenerate rings with 400, which used to leave
// a ghost feature stuck on the map and confuse the user.
function isValidDrawnPolygon(geom) {
  if (!geom || geom.getType() !== "Polygon") return false;
  const ring = geom.getCoordinates()[0];
  if (!ring || ring.length < 3) return false;
  // Shoelace area in CRS units — degenerate if ~0.
  let area2 = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    area2 += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
  }
  return Math.abs(area2 / 2) > 1e-9;
}

// ---- Initialisation -------------------------------------------------------

function initMap() {
  vectorSource = new ol.source.Vector();

  vectorLayer = new ol.layer.Vector({
    source: vectorSource,
    style: styleForFeature,
    // Pre-render a margin around the viewport so hit-detection has coloured
    // pixels ready even for polygons slightly off-screen.
    renderBuffer: 200,
  });

  map = new ol.Map({
    target: "map",
    layers: [vectorLayer],
    view: new ol.View({
      center: [0, 0],
      zoom: 2,
      projection: "EPSG:3857",
    }),
  });

  // Two interactions, but only one active at a time — like QGIS. Starting in
  // SELECT mode means a plain click never accidentally starts a new polygon
  // (which was the root cause of "click draws instead of selects").
  draw = new ol.interaction.Draw({ source: vectorSource, type: "Polygon" });
  modify = new ol.interaction.Modify({ source: vectorSource });
  // Modify (vertex drag) stays on always; Draw is toggled by the toolbar.
  map.addInteraction(modify);
  draw.setActive(false);
  map.addInteraction(draw);

  // New polygon drawn by the user. After finish, drop out of draw mode.
  draw.on("drawstart", () => {
    // Guard against a degenerate second click starting a new polygon.
    setStatus("drawing — click to add vertices, double-click to finish (Esc to cancel)");
  });
  draw.on("drawend", (evt) => {
    const feature = evt.feature;
    // Validate locally before sending: an empty/self-intersecting polygon
    // makes the backend return 400 and leaves a ghost feature on the map.
    const ok = isValidDrawnPolygon(feature.getGeometry());
    if (!ok) {
      vectorSource.removeFeature(feature);
      setStatus("invalid polygon (needs >=3 vertices and non-zero area) — discarded");
      setDrawMode(false);
      return;
    }
    sendAddPolygon(feature.getGeometry());
    setDrawMode(false);
  });

  // Vertex dragged.
  modify.on("modifyend", (evt) => {
    const feature = evt.features.getArray()[0];
    if (!feature) return;
    const idx = feature.get("index");
    if (idx === undefined || idx === null) return;
    sendReplacePolygon(idx, feature.getGeometry());
  });

  // Click on a polygon selects it. A generous hitTolerance lets the user
  // click slightly outside a thin outline and still select the polygon.
  map.on("singleclick", (evt) => {
    let hit = null;
    map.forEachFeatureAtPixel(
      evt.pixel,
      (feat) => {
        hit = feat;
        return true;
      },
      { hitTolerance: 12 }
    );
    const idx = hit ? hit.get("index") : null;
    selectedIndex = typeof idx === "number" ? idx : null;
    vectorLayer.changed();
    // Refresh the bottom chip panel so the selected chip is highlighted, and
    // update the Delete button's enabled state.
    renderPolygonChips(currentCollection());
    if (selectedIndex === null) {
      setStatus(`${vectorSource.getFeatures().length} glacier(s) — nothing selected`);
    } else {
      const name = hit.get("name") || `glacier_${selectedIndex + 1}`;
      setStatus(`selected: ${name} — Delete to remove, drag vertices to edit`);
    }
  });

  // Keyboard shortcuts:
  //   Delete/Backspace — remove the selected polygon
  //   Esc              — cancel current drawing, return to select mode
  //   N                — start drawing a new polygon
  window.addEventListener("keydown", (evt) => {
    if (evt.key === "Escape" && drawMode) {
      setDrawMode(false);
      evt.preventDefault();
      return;
    }
    if ((evt.key === "n" || evt.key === "N") && !drawMode) {
      setDrawMode(true);
      evt.preventDefault();
      return;
    }
    const canDelete =
      (evt.key === "Delete" || evt.key === "Backspace") &&
      typeof selectedIndex === "number";
    if (!canDelete) return;
    sendDeletePolygon(selectedIndex);
    evt.preventDefault();
  });

  // Initial state: one call gets BOTH the current GeoTIFF (if any) and the
  // polygons. If a raster is loaded, we must restore its CRS and layer
  // BEFORE the polygons are read, otherwise their coordinates would be
  // interpreted as Web Mercator and land near the pole after a reload.
  fetch(`${BACKEND}/api/state`)
    .then((r) => r.json())
    .then((state) => {
      if (state.geotiff) {
        try {
          applyGeoTiffToMap(state.geotiff);
          setStatus(`raster restored: ${state.geotiff.filename}`);
        } catch (err) {
          console.error("failed to restore raster:", err);
          setStatus(`raster restore failed: ${err.message || err}`);
        }
      } else {
        setStatus("ready — no raster loaded");
      }
      // applyGeoTiffToMap already cleared vectorSource if the CRS changed,
      // so the polygons from the server are now drawn in the right CRS.
      applyCollectionFromServer({ type: "polygons", polygons: state.polygons });
    })
    .catch((err) => {
      console.error("boot: /api/state failed:", err);
      setStatus("failed to load initial state");
    });

  connectWebSocket();
}

// ---- Server round trips ---------------------------------------------------

function sendAddPolygon(geometry) {
  const gj = geometryToGeoJSON(geometry);
  fetch(`${BACKEND}/api/polygons`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ geometry: gj }),
  })
    .then(async (r) => {
      if (!r.ok) {
        // 400 usually means the ring was invalid (self-intersecting etc.).
        // Re-fetch the canonical state so the ghost feature disappears from
        // the map instead of lingering and confusing the user.
        const text = await r.text();
        console.error("add polygon rejected:", r.status, text);
        setStatus(`polygon rejected (${r.status}) — reloading state`);
        const fresh = await fetch(`${BACKEND}/api/polygons`).then((x) => x.json());
        applyCollectionFromServer({ type: "polygons", ...fresh });
        return null;
      }
      return r.json();
    })
    .then((body) => {
      if (body) applyCollectionFromServer({ type: "polygons", ...body });
    })
    .catch((err) => console.error("add polygon failed:", err));
}

function sendReplacePolygon(index, geometry) {
  if (suppressServerSync) return;
  const gj = geometryToGeoJSON(geometry);
  fetch(`${BACKEND}/api/polygons`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ geometry: gj, index }),
  })
    .then((r) => r.json())
    .then((body) => applyCollectionFromServer({ type: "polygons", ...body }))
    .catch((err) => console.error("replace polygon failed:", err));
}

function sendDeletePolygon(index) {
  fetch(`${BACKEND}/api/polygons/${index}`, { method: "DELETE" })
    .then((r) => r.json())
    .then((body) => {
      selectedIndex = null;
      applyCollectionFromServer({ type: "polygons", ...body });
    })
    .catch((err) => console.error("delete polygon failed:", err));
}

function sendUndo() {
  fetch(`${BACKEND}/api/history/undo`, { method: "POST" })
    .then((r) => r.json())
    .then((body) => applyCollectionFromServer({ type: "polygons", ...body }))
    .catch((err) => console.error("undo failed:", err));
}

function sendRedo() {
  fetch(`${BACKEND}/api/history/redo`, { method: "POST" })
    .then((r) => r.json())
    .then((body) => applyCollectionFromServer({ type: "polygons", ...body }))
    .catch((err) => console.error("redo failed:", err));
}

function exportShapefile() {
  fetch(`${BACKEND}/api/export/shapefile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ basename: "glaciers" }),
  })
    .then(async (r) => {
      if (!r.ok) {
        const text = await r.text();
        throw new Error(`${r.status}: ${text}`);
      }
      return r.blob();
    })
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "glaciers.zip";
      a.click();
      URL.revokeObjectURL(url);
    })
    .catch((err) => {
      console.error("export failed:", err);
      setInfo(`Export failed: ${err.message || err}`);
    });
}

// ---- Server -> client -----------------------------------------------------

function geometryToGeoJSON(geometry) {
  return new ol.format.GeoJSON({
    dataProjection: currentCrsCode,
    featureProjection: currentCrsCode,
  }).writeGeometryObject(geometry);
}

function applyCollectionFromServer(msg) {
  const fc = msg.polygons;
  if (!fc || fc.type !== "FeatureCollection") return;

  suppressServerSync = true;
  try {
    vectorSource.clear();
    const format = new ol.format.GeoJSON({
      dataProjection: currentCrsCode,
      featureProjection: currentCrsCode,
    });
    fc.features.forEach((feature) => {
      const olFeature = format.readFeature(feature);
      olFeature.set("index", feature.properties.index);
      olFeature.set("name", feature.properties.name);
      vectorSource.addFeature(olFeature);
    });
    const count = fc.features.length;
    setStatus(`${count} glacier${count === 1 ? "" : "s"}`);
    // Rebuild the bottom chip panel and refresh the Delete button state.
    renderPolygonChips(fc);
  } finally {
    suppressServerSync = false;
  }
}

// ---- Bottom polygon panel -------------------------------------------------
//
// Renders one chip per polygon. Chip click selects the polygon (same effect
// as clicking it on the map). Selected chip is highlighted; the Delete
// button enables only when something is selected. The chip row scrolls
// horizontally when there are more glaciers than fit on screen.

// Build a FeatureCollection-shaped object from the current vector source.
// Used when we need to re-render chips outside of applyCollectionFromServer
// (e.g. after a map click changed the selection).
function currentCollection() {
  const features = vectorSource.getFeatures().map((f) => ({
    type: "Feature",
    properties: { index: f.get("index"), name: f.get("name") },
  }));
  return { type: "FeatureCollection", features };
}

function renderPolygonChips(fc) {
  const host = document.getElementById("polygon-chips");
  if (!host) return;
  host.innerHTML = "";
  if (!fc || fc.type !== "FeatureCollection") {
    updateDeleteButton();
    return;
  }
  fc.features.forEach((feature) => {
    const idx = feature.properties.index;
    const name = feature.properties.name || `glacier_${idx + 1}`;
    const color = COLORS[idx % COLORS.length];

    const chip = document.createElement("div");
    chip.className = "chip" + (idx === selectedIndex ? " chip-selected" : "");
    chip.dataset.index = String(idx);
    chip.title = `${name} — click to select, Delete to remove`;

    const swatch = document.createElement("span");
    swatch.className = "chip-swatch";
    swatch.style.background = color;
    chip.appendChild(swatch);

    const label = document.createElement("span");
    label.textContent = `${idx + 1}. ${name}`;
    chip.appendChild(label);

    chip.addEventListener("click", () => {
      selectedIndex = idx;
      vectorLayer.changed();
      renderPolygonChips(fc);
      setStatus(`selected: ${name} — Delete to remove, drag vertices to edit`);
    });

    host.appendChild(chip);
  });
  updateDeleteButton();
  // Keep the modal table in sync — it may be open right now.
  renderModalTable(fc);
}

function updateDeleteButton() {
  const btn = document.getElementById("btn-delete-selected");
  if (!btn) return;
  btn.disabled = typeof selectedIndex !== "number";
}

// ---- Modal with the full polygon list -------------------------------------
//
// The bottom chip row works for a handful of glaciers but scrolls off screen
// for 100+. The modal is a proper table with a search box, so a big session
// stays navigable. Clicking a row selects that polygon on the map; the × on
// a row removes it.

function openPolygonModal() {
  const modal = document.getElementById("polygon-modal");
  if (!modal) return;
  renderModalTable(currentCollection());
  if (!modal.open) modal.showModal();
  const search = document.getElementById("modal-search");
  if (search) search.focus();
}

function closePolygonModal() {
  const modal = document.getElementById("polygon-modal");
  if (modal && modal.open) modal.close();
}

function renderModalTable(fc) {
  const tbody = document.getElementById("modal-tbody");
  if (!tbody) return;
  const features = (fc && fc.features) || [];

  const searchEl = document.getElementById("modal-search");
  const query = ((searchEl && searchEl.value) || "").trim().toLowerCase();

  const countEl = document.getElementById("modal-count");
  if (countEl) countEl.textContent = String(features.length);

  const emptyEl = document.getElementById("modal-empty");

  const visible = features.filter((f) => {
    const name = String((f.properties && f.properties.name) || "").toLowerCase();
    return query === "" || name.includes(query);
  });

  if (emptyEl) {
    // Show the empty hint only when there are no polygons at all, not when
    // the search filtered everything out (then the table header is enough).
    emptyEl.hidden = features.length > 0;
  }

  tbody.innerHTML = "";
  for (const feature of visible) {
    const idx = feature.properties ? feature.properties.index : null;
    if (typeof idx !== "number") continue;
    const name = (feature.properties && feature.properties.name) || `glacier_${idx + 1}`;
    // The closed ring has the first vertex repeated at the end; subtract it.
    let vertices = 0;
    const coords =
      feature.geometry && feature.geometry.coordinates && feature.geometry.coordinates[0];
    if (Array.isArray(coords)) vertices = Math.max(0, coords.length - 1);

    const tr = document.createElement("tr");
    tr.dataset.index = String(idx);
    if (idx === selectedIndex) tr.classList.add("row-selected");

    const tdNum = document.createElement("td");
    tdNum.className = "modal-num";
    tdNum.textContent = String(idx + 1);

    const tdName = document.createElement("td");
    tdName.className = "modal-name";
    tdName.textContent = name;
    tdName.title = name;

    const tdV = document.createElement("td");
    tdV.className = "modal-vertices";
    tdV.textContent = String(vertices);

    const tdA = document.createElement("td");
    tdA.className = "modal-actions";
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "modal-delete";
    delBtn.textContent = "×";
    delBtn.title = `Delete ${name}`;
    delBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      sendDeletePolygon(idx);
    });
    tdA.appendChild(delBtn);

    tr.appendChild(tdNum);
    tr.appendChild(tdName);
    tr.appendChild(tdV);
    tr.appendChild(tdA);

    tr.addEventListener("click", () => {
      selectedIndex = idx;
      vectorLayer.changed();
      renderPolygonChips(currentCollection());
      renderModalTable(currentCollection());
      setStatus(`selected: ${name} — Delete to remove, drag vertices to edit`);
    });

    tbody.appendChild(tr);
  }
}

// ---- GeoTIFF upload -------------------------------------------------------

async function handleFileUpload(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  setInfo(`Uploading ${file.name}...`);
  setStatus("uploading...");

  const form = new FormData();
  form.append("file", file);

  try {
    const resp = await fetch(`${BACKEND}/api/geotiff/upload`, {
      method: "POST",
      body: form,
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`${resp.status}: ${text}`);
    }
    const meta = await resp.json();
    applyGeoTiffToMap(meta);
    setInfo(
      `${meta.filename} — ${meta.width}×${meta.height}px, ` +
        `${meta.bands} band(s), ${meta.crs_name || "CRS: " + meta.crs_epsg}`
    );
  } catch (err) {
    console.error("GeoTIFF upload/apply failed:", err);
    const msg = err && err.message ? err.message : String(err);
    setInfo(`Upload failed: ${msg}`);
    setStatus("error");
  } finally {
    event.target.value = "";
  }
}

function applyGeoTiffToMap(meta) {
  if (!meta) throw new Error("Server returned empty metadata.");
  if (typeof proj4 === "undefined") {
    throw new Error("proj4js not loaded (blocked CDN?) — cannot register CRS.");
  }

  const crsCode = `RASTER:${meta.id}`;
  const def =
    meta.crs_proj4 && meta.crs_proj4.trim().length > 0 ? meta.crs_proj4 : meta.crs_wkt;
  if (!def) {
    throw new Error("Server returned neither crs_proj4 nor crs_wkt — cannot register CRS.");
  }
  try {
    proj4.defs(crsCode, def);
  } catch (e) {
    throw new Error(
      `proj4.defs failed for ${crsCode}: ${e && e.message ? e.message : e}`
    );
  }

  const extent = meta.bounds;
  const wktSaysProjected =
    meta.crs_wkt && (meta.crs_wkt.includes("PROJCS") || /\bunits\s*=\s*m/.test(meta.crs_wkt));
  const units = wktSaysProjected ? "m" : "degrees";

  // NOTE: we deliberately do NOT set `extent` on the custom projection.
  // When a projection has an extent, OpenLayers constrains the view's
  // centre to that extent. If the projection's extent is not recognised
  // (e.g. proj4js has just registered it and OL is still using a stale
  // cache), the view collapses to [0, 0] and any later setCenter() call
  // is silently clamped back. Removing the extent lets the view centre
  // wherever we tell it to; the raster layer already has its own extent
  // so tiles outside the raster simply are not drawn.
  const projection = new ol.proj.Projection({
    code: crsCode,
    units,
    axisOrientation: "enu",
  });

  // Decide whether the polygon geometry needs to be cleared. If the new
  // raster has the SAME CRS as the previous one (the common case: user
  // re-uploads the same scene), the existing polygons are still valid and
  // should be kept. Only when the CRS actually changes do we clear them,
  // because their coordinates would be meaningless in the new projection.
  const crsChanged = crsCode !== currentCrsCode;

  // Ask the backend for the path of the source GeoTIFF on disk. titiler
  // reads directly from that file when serving tiles.
  // We do a synchronous XHR here only because applyGeoTiffToMap is called
  // from a non-async code path and we need the path before building the
  // tile layer. Local single-user tool, so a sync request is acceptable.
  let cogPath = null;
  try {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", `${BACKEND}/api/geotiff/${meta.id}/cog-url`, false);
    xhr.send(null);
    if (xhr.status === 200) {
      cogPath = JSON.parse(xhr.responseText).url;
    } else {
      throw new Error(`${xhr.status} ${xhr.statusText}`);
    }
  } catch (err) {
    throw new Error(`Could not obtain COG path from backend: ${err.message || err}`);
  }
  if (!cogPath) {
    throw new Error("Backend returned no COG path — cannot build the tile layer.");
  }

  // Build a tile grid in the raster's OWN CRS. z=0 is a single tile for
  // Use a single ImageStatic rather than an XYZ tile pyramid.
  //
  // Why: for rasters up to ~4096 px the preview PNG already has full (or
  // even over-) resolution, and a single image cannot develop the seams
  // that occur when a non-square raster is cut into square tiles (the
  // tile grid and the raster extent do not share aspect ratio). If we ever
  // need to display a Sentinel-2-scale scene (>4096 px) we will bring the
  // tile path back, in the raster CRS, with a matched tile grid.
  const width = Math.abs(extent[2] - extent[0]);
  const height = Math.abs(extent[3] - extent[1]);

  // ---- Raster basemap: our own rasterio-backed tile server --------------
  //
  // We used to route this through titiler, but rio-tiler 9.x loses the
  // GeoTIFF's CRS through its own Reader (image.crs = None), which makes
  // titiler crash in CRS_to_uri with a 500 on every tile. Our own endpoint
  // `/api/geotiff/{id}/tile/{z}/{x}/{y}.png` uses rasterio.open() directly
  // (which sees the CRS correctly) and cuts tiles in the raster's OWN CRS.
  //
  // Because the tiles are in the raster CRS — not Web Mercator — we build
  // the OL tile grid in that same CRS. `z=0` is one tile for the whole
  // raster; `z=N` is 2**N by 2**N tiles. The origin is the raster's
  // top-left corner, matching rasterio's row direction (rows grow down).
  const TILE_SIZE = 256;
  const MAX_Z = 20;
  const maxSpan = Math.max(width, height) || 1;
  const resolutions = [];
  for (let z = 0; z <= MAX_Z; z++) {
    resolutions.push(maxSpan / (Math.pow(2, z) * TILE_SIZE));
  }
  const rasterTileGrid = new ol.tilegrid.TileGrid({
    origin: [extent[0], extent[3]],
    resolutions,
    tileSize: TILE_SIZE,
  });

  if (rasterLayer) map.removeLayer(rasterLayer);
  rasterLayer = new ol.layer.Tile({
    // Clip the layer to the raster's own extent so OpenLayers never asks
    // for tiles outside the raster. Without this, panning around the edges
    // makes OL request tiles the backend returns as fully transparent, but
    // which OL still paints over the black canvas — visible as black bars.
    extent,
    source: new ol.source.TileImage({
      projection,
      tileGrid: rasterTileGrid,
      crossOrigin: "anonymous",
      tileUrlFunction: (tileCoord) => {
        const [z, x, tileY] = tileCoord;
        // Both our rasterTileGrid (origin at raster top-left) and the
        // backend endpoint count rows downward from the top-left corner:
        // tile (0,0) is the upper-left tile, y grows down. No Y flip is
        // needed — the OL tile grid already matches rasterio's row order.
        return (
          `${BACKEND}/api/geotiff/${meta.id}/tile/` +
          `${z}/${x}/${tileY}.png`
        );
      },
    }),
  });
  map.getLayers().insertAt(0, rasterLayer);

  currentCrsCode = crsCode;
  if (crsChanged) {
    vectorSource.clear();
    selectedIndex = null;
  }

  const view = new ol.View({
    projection,
    center: [(extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2],
    constrainResolution: false,
    // Clamp the view to the tile pyramid. `resolutions[0]` is z=0, where the
    // whole raster fits into a single 256x256 tile. If the view is allowed
    // to zoom out past that (e.g. a small browser window with a huge raster
    // — `view.fit()` computes its own resolution from the viewport), the
    // tile grid clamps z to 0 for every query and stretches the SAME z=0
    // tile across the whole screen. That is what produced the visible
    // "tiles mosaic" — the same tile painted many times.
    maxResolution: resolutions[0],
    // Keep the raster's full detail available: allow zooming to the native
    // pixel resolution and beyond, but never let the view drift so far out
    // that the raster becomes a dot.
    minResolution: Math.max(width / 100000, 1e-9),
  });
  map.setView(view);

  // Fit the *whole* extent into the map viewport, leaving a small margin.
  // Using view.fit() (instead of hand-computed resolution) accounts for the
  // actual aspect ratio of both the raster and the browser window, so a tall
  // raster is no longer squeezed into a narrow strip.
  view.fit(extent, {
    size: map.getSize(),
    padding: [20, 20, 20, 20],
    constrainResolution: false,
  });
  // `view.fit()` recomputes the resolution from the viewport size, ignoring
  // the `maxResolution` we set above. Explicitly snap the view back to z=0
  // (coarsest tile) and the raster's centre. This is the actual fix for
  // the tiled-mosaic look: without this clamp OL ends up with a resolution
  // coarser than any tile level, so every tile request resolves to z=0 and
  // the SAME z=0 tile is stretched across the whole viewport.
  //
  // We do the clamp unconditionally (not with `if (r > res[0])`) and log
  // the values so we can verify what OL is actually doing.
  const targetResolution = resolutions[0];
  const rasterCenter = [
    (extent[0] + extent[2]) / 2,
    (extent[1] + extent[3]) / 2,
  ];
  // eslint-disable-next-line no-console
  console.log("[glacier] ================== APPLY GEOTIFF ==================");
  // eslint-disable-next-line no-console
  console.log("[glacier] meta:", JSON.stringify(meta, null, 2));
  // eslint-disable-next-line no-console
  console.log("[glacier] extent:", extent);
  // eslint-disable-next-line no-console
  console.log("[glacier] width:", width, "height:", height, "maxSpan:", maxSpan);
  // eslint-disable-next-line no-console
  console.log("[glacier] resolutions.length:", resolutions.length);
  // eslint-disable-next-line no-console
  console.log("[glacier] resolutions[0..4]:", resolutions.slice(0, 5));
  // eslint-disable-next-line no-console
  console.log("[glacier] targetResolution (= resolutions[0]):", targetResolution);
  // eslint-disable-next-line no-console
  console.log("[glacier] before clamp, view.resolution:", view.getResolution(), "view.center:", view.getCenter());
  view.setResolution(targetResolution);
  view.setCenter(rasterCenter);
  // eslint-disable-next-line no-console
  console.log("[glacier] after clamp, view.resolution:", view.getResolution(), "view.center:", view.getCenter());

  // Populate the debug HUD with this raster's identity and metadata. The
  // view-related rows (zoom / resolution / center / mouse) update on their
  // own via the subscriptions installed in installHud().
  updateHudRaster(meta);
}

// ---- Buttons --------------------------------------------------------------

function wireButtons() {
  const drawBtn = document.getElementById("btn-draw");
  const undoBtn = document.getElementById("btn-undo");
  const redoBtn = document.getElementById("btn-redo");
  const exportBtn = document.getElementById("btn-export");
  const deleteBtn = document.getElementById("btn-delete-selected");
  const showAllBtn = document.getElementById("btn-show-all");
  const modalCloseBtn = document.getElementById("modal-close");
  const modalSearch = document.getElementById("modal-search");
  // "Draw new polygon" toggles draw mode on/off. Clicking again cancels.
  if (drawBtn) drawBtn.addEventListener("click", () => setDrawMode(!drawMode));
  if (undoBtn) undoBtn.addEventListener("click", sendUndo);
  if (redoBtn) redoBtn.addEventListener("click", sendRedo);
  if (exportBtn) exportBtn.addEventListener("click", exportShapefile);
  if (deleteBtn) {
    deleteBtn.addEventListener("click", () => {
      if (typeof selectedIndex !== "number") return;
      sendDeletePolygon(selectedIndex);
    });
  }
  if (showAllBtn) showAllBtn.addEventListener("click", openPolygonModal);
  if (modalCloseBtn) modalCloseBtn.addEventListener("click", closePolygonModal);
  if (modalSearch) {
    modalSearch.addEventListener("input", () => renderModalTable(currentCollection()));
  }
  // Esc inside the modal closes it via <dialog>'s default, but the × also
  // has to work when the browser translates Esc into "close" natively.
}

// ---- Debug HUD ------------------------------------------------------------
//
// Small overlay in the bottom-right corner that shows the view state
// (zoom / resolution / center / mouse) and the loaded raster's identity
// (id, filename, size, bands, CRS, extent, tile URL). Press D to toggle.
// The raster block is filled on upload; the view block updates on every
// zoom / pan; the mouse block updates on pointer move.
const hudRoot = document.getElementById("debug-hud");
const hudEls = {
  zoom: document.getElementById("hud-zoom"),
  resolution: document.getElementById("hud-resolution"),
  center: document.getElementById("hud-center"),
  mouse: document.getElementById("hud-mouse"),
  id: document.getElementById("hud-id"),
  filename: document.getElementById("hud-filename"),
  size: document.getElementById("hud-size"),
  bands: document.getElementById("hud-bands"),
  crs: document.getElementById("hud-crs"),
  extent: document.getElementById("hud-extent"),
  tileUrl: document.getElementById("hud-tile-url"),
};

function hudFmt(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(digits);
}

function updateHudView() {
  if (!map || !hudEls.zoom) return;
  const view = map.getView();
  const z = view.getZoom();
  const res = view.getResolution();
  const c = view.getCenter() || [NaN, NaN];
  hudEls.zoom.textContent = z === undefined ? "—" : z.toFixed(2);
  hudEls.resolution.textContent =
    res === undefined ? "—" : res.toExponential(3);
  hudEls.center.textContent = `[${hudFmt(c[0])}, ${hudFmt(c[1])}]`;
  // eslint-disable-next-line no-console
  console.log("[glacier] view changed: zoom=", z, "resolution=", res, "center=", c);
}

function updateHudMouse(evt) {
  if (!hudEls.mouse) return;
  const [x, y] = evt.coordinate;
  hudEls.mouse.textContent = `[${hudFmt(x)}, ${hudFmt(y)}]`;
}

function updateHudRaster(meta) {
  if (!meta || !hudEls.id) return;
  hudEls.id.textContent = meta.id || "—";
  hudEls.filename.textContent = meta.filename || "—";
  hudEls.size.textContent = `${meta.width} × ${meta.height} px`;
  hudEls.bands.textContent =
    meta.bands != null ? String(meta.bands) : "—";
  const epsg = meta.crs_epsg != null ? `EPSG:${meta.crs_epsg}` : "(no EPSG)";
  hudEls.crs.textContent = `${meta.crs_name || "unknown"} · ${epsg}`;
  hudEls.extent.textContent =
    `[${hudFmt(meta.bounds[0])}, ${hudFmt(meta.bounds[1])}, ` +
    `${hudFmt(meta.bounds[2])}, ${hudFmt(meta.bounds[3])}]`;
  hudEls.tileUrl.textContent =
    `/api/geotiff/${meta.id}/tile/{z}/{x}/{y}.png`;
}

function installHud() {
  if (!map || !hudEls.zoom) return;
  map.getView().on("change:resolution", updateHudView);
  map.getView().on("change:center", updateHudView);
  map.on("pointermove", updateHudMouse);
  const toggleBtn = document.getElementById("hud-toggle");
  if (toggleBtn && hudRoot) {
    toggleBtn.addEventListener("click", () => {
      hudRoot.classList.toggle("hud-collapsed");
      toggleBtn.textContent = hudRoot.classList.contains("hud-collapsed")
        ? "▸"
        : "▾";
    });
  }
  document.addEventListener("keydown", (e) => {
    if ((e.key === "d" || e.key === "D") && hudRoot) {
      const tag = e.target && e.target.tagName;
      if (!tag || !/^(INPUT|TEXTAREA)$/.test(tag)) {
        hudRoot.classList.toggle("hud-hidden");
      }
    }
  });
  updateHudView();
}

// ---- Boot -----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initMap();
  wireButtons();
  installHud();
  if (fileInput) {
    fileInput.addEventListener("change", handleFileUpload);
  }
});
