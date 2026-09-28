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

function styleForFeature(feature) {
  const idx = feature.get("index");
  const isSelected = idx === selectedIndex;
  const color = COLORS[(idx ?? 0) % COLORS.length];
  return new ol.style.Style({
    stroke: new ol.style.Stroke({ color, width: isSelected ? 3 : 2 }),
    fill: new ol.style.Fill({
      color: isSelected ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0)",
    }),
  });
}

// ---- Initialisation -------------------------------------------------------

function initMap() {
  vectorSource = new ol.source.Vector();

  vectorLayer = new ol.layer.Vector({
    source: vectorSource,
    style: styleForFeature,
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

  draw = new ol.interaction.Draw({ source: vectorSource, type: "Polygon" });
  modify = new ol.interaction.Modify({ source: vectorSource });
  map.addInteraction(draw);
  map.addInteraction(modify);

  // New polygon drawn by the user.
  draw.on("drawend", (evt) => {
    const feature = evt.feature;
    // The backend assigns the index; we optimistically fetch it after POST.
    sendAddPolygon(feature.getGeometry());
  });

  // Vertex dragged.
  modify.on("modifyend", (evt) => {
    const feature = evt.features.getArray()[0];
    if (!feature) return;
    const idx = feature.get("index");
    if (idx === undefined || idx === null) return;
    sendReplacePolygon(idx, feature.getGeometry());
  });

  // Click on a polygon selects it.
  map.on("singleclick", (evt) => {
    let hit = null;
    map.forEachFeatureAtPixel(evt.pixel, (feat) => {
      hit = feat;
      return true;
    });
    selectedIndex = hit ? hit.get("index") : null;
    vectorLayer.changed();
  });

  // Delete key removes the selected polygon.
  window.addEventListener("keydown", (evt) => {
    if ((evt.key === "Delete" || evt.key === "Backspace") && selectedIndex !== null) {
      sendDeletePolygon(selectedIndex);
      evt.preventDefault();
    }
  });

  fetch(`${BACKEND}/api/polygons`)
    .then((r) => r.json())
    .then((body) => applyCollectionFromServer({ type: "polygons", ...body }))
    .catch(() => {});

  connectWebSocket();
  setStatus("ready — no raster loaded");
}

// ---- Server round trips ---------------------------------------------------

function sendAddPolygon(geometry) {
  const gj = geometryToGeoJSON(geometry);
  fetch(`${BACKEND}/api/polygons`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ geometry: gj }),
  })
    .then((r) => r.json())
    .then((body) => applyCollectionFromServer({ type: "polygons", ...body }))
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
  } finally {
    suppressServerSync = false;
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

  const projection = new ol.proj.Projection({
    code: crsCode,
    units,
    extent,
    axisOrientation: "enu",
  });

  if (rasterLayer) map.removeLayer(rasterLayer);
  rasterLayer = new ol.layer.Image({
    source: new ol.source.ImageStatic({
      url: `${BACKEND}/api/geotiff/${meta.id}/preview.png`,
      imageExtent: extent,
      projection,
    }),
  });
  map.getLayers().insertAt(0, rasterLayer);

  currentCrsCode = crsCode;
  vectorSource.clear();
  selectedIndex = null;

  const center = [(extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2];
  const span = Math.max(Math.abs(extent[2] - extent[0]), Math.abs(extent[3] - extent[1])) || 1;
  map.setView(
    new ol.View({
      projection,
      center,
      resolution: span / 800,
      constrainResolution: false,
    })
  );
}

// ---- Buttons --------------------------------------------------------------

function wireButtons() {
  const undoBtn = document.getElementById("btn-undo");
  const redoBtn = document.getElementById("btn-redo");
  const exportBtn = document.getElementById("btn-export");
  if (undoBtn) undoBtn.addEventListener("click", sendUndo);
  if (redoBtn) redoBtn.addEventListener("click", sendRedo);
  if (exportBtn) exportBtn.addEventListener("click", exportShapefile);
}

// ---- Boot -----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initMap();
  wireButtons();
  if (fileInput) {
    fileInput.addEventListener("change", handleFileUpload);
  }
});
