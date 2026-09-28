// glacier-mcp frontend: OpenLayers map + GeoTIFF basemap + polygon editing.
//
// Responsibilities:
//   * show a loaded GeoTIFF as a static image layer in its own CRS
//   * let the user draw and modify the glacier polygon with the mouse
//   * POST edits to the backend so the shared PolygonState sees them
//   * listen on /ws for edits coming from the agent and re-render
//
// Coordinate handling is subtle. The backend's PolygonState and the
// shapefile export both expect coordinates in the raster's native CRS
// (UTM zone, Polar Stereographic, etc), not in the browser's Web
// Mercator. So once a GeoTIFF is loaded we switch the map view and all
// draw/modify interactions to that CRS. Before any raster is loaded the
// map falls back to EPSG:3857 with an empty background.

const BACKEND = window.location.origin;

// Register proj4 with OpenLayers once, so that any CRS we add later via
// proj4.defs() is automatically visible to ol.proj.* calls.
// Guarded so a blocked CDN does not silently kill the whole script.
if (typeof proj4 !== "undefined" && ol.proj.proj4) {
  ol.proj.proj4.register(proj4);
} else {
  console.warn("proj4js not available at load time; raster CRS will be unavailable.");
}

// ---- WebSocket sync -------------------------------------------------------

let ws = null;

function connectWebSocket() {
  ws = new WebSocket(`ws://${window.location.host}/ws`);
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "polygon") {
      applyPolygonFromServer(msg.polygon);
    }
  };
  ws.onclose = () => {
    // Simple reconnect with backoff. The server is stateless per client.
    setTimeout(connectWebSocket, 1000);
  };
}

// ---- Map state ------------------------------------------------------------

let map = null;
let vectorSource = null;
let vectorLayer = null;
let draw = null;
let modify = null;
let rasterLayer = null; // ol.layer.Image for the GeoTIFF preview
let currentCrsCode = "EPSG:3857"; // updated after a GeoTIFF upload

// ---- DOM handles ----------------------------------------------------------

const fileInput = document.getElementById("geotiff-file");
const infoSpan = document.getElementById("geotiff-info");
const statusSpan = document.getElementById("status");

function setStatus(text) {
  if (statusSpan) statusSpan.textContent = text;
}

function setInfo(text) {
  if (infoSpan) infoSpan.textContent = text;
}

// ---- Initialisation -------------------------------------------------------

function initMap() {
  vectorSource = new ol.source.Vector();

  vectorLayer = new ol.layer.Vector({
    source: vectorSource,
    style: new ol.style.Style({
      stroke: new ol.style.Stroke({ color: "#00e0ff", width: 2 }),
      fill: new ol.style.Fill({ color: "rgba(0,224,255,0.15)" }),
    }),
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

  draw.on("drawend", (evt) => {
    // OL already added evt.feature to vectorSource when Draw({source}) is
    // configured. Calling clear() + addFeature() re-adds the same object
    // and triggers: "The passed 'feature' was already added to the source".
    // Instead, drop every *other* feature and keep the fresh one.
    const keep = evt.feature;
    for (const f of vectorSource.getFeatures().slice()) {
      if (f !== keep) vectorSource.removeFeature(f);
    }
    sendPolygon(keep.getGeometry());
  });

  modify.on("modifyend", () => {
    const feature = vectorSource.getFeatures()[0];
    if (!feature) return;
    sendPolygon(feature.getGeometry());
  });

  // Initial polygon from server (probably empty on first load).
  fetch(`${BACKEND}/api/polygon`)
    .then((r) => r.json())
    .then(applyPolygonFromServer)
    .catch(() => {});

  connectWebSocket();
  setStatus("ready — no raster loaded");
}

// ---- Polygon helpers ------------------------------------------------------

// GeoJSON Feature written in the map's *current* CRS. Since we keep the
// view in the same CRS as the loaded GeoTIFF, that means the coordinates
// we send are already in the raster's native CRS - exactly what the
// backend expects for shapefile export.
function sendPolygon(geometry) {
  const geojson = new ol.format.GeoJSON({
    dataProjection: currentCrsCode,
    featureProjection: currentCrsCode,
  }).writeGeometryObject(geometry);

  fetch(`${BACKEND}/api/polygon`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: "Feature", properties: {}, geometry: geojson }),
  }).catch(() => {});
}

function applyPolygonFromServer(geojson) {
  if (!geojson || !geojson.geometry || !geojson.geometry.coordinates?.length) {
    return;
  }
  const feature = new ol.format.GeoJSON({
    dataProjection: currentCrsCode,
    featureProjection: currentCrsCode,
  }).readFeature(geojson);
  vectorSource.clear();
  vectorSource.addFeature(feature);
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
    setStatus("raster loaded");
  } catch (err) {
    // Surface the real error in DevTools too - the info bar is small.
    console.error("GeoTIFF upload/apply failed:", err);
    const msg = err && err.message ? err.message : String(err);
    setInfo(`Upload failed: ${msg}`);
    setStatus("error");
  } finally {
    // Allow re-selecting the same file later.
    event.target.value = "";
  }
}

function applyGeoTiffToMap(meta) {
  // Sanity checks first, so we surface a useful message instead of a
  // bare TypeError from inside proj4/OL.
  if (!meta) {
    throw new Error("Server returned empty metadata.");
  }
  if (typeof proj4 === "undefined") {
    throw new Error("proj4js not loaded (blocked CDN?) — cannot register CRS.");
  }

  const crsCode = `RASTER:${meta.id}`;
  // proj4js parses raw proj4 strings and WKT1_GDAL, but NOT WKT2. The
  // backend now sends both; we prefer the proj4 string because it always
  // works, and fall back to WKT only if the proj4 string is missing.
  const def = meta.crs_proj4 && meta.crs_proj4.trim().length > 0
    ? meta.crs_proj4
    : meta.crs_wkt;
  if (!def) {
    throw new Error("Server returned neither crs_proj4 nor crs_wkt — cannot register CRS.");
  }
  try {
    proj4.defs(crsCode, def);
  } catch (e) {
    throw new Error(
      `proj4.defs failed for ${crsCode}: ${e && e.message ? e.message : e}` +
      ` (def was: ${String(def).slice(0, 120)}...)`
    );
  }

  // Build an OL projection object. Units are chosen from the WKT when
  // possible; a geographic CRS is in degrees, a projected one in metres.
  const extent = meta.bounds; // [left, bottom, right, top]
  const wktSaysProjected =
    meta.crs_wkt.includes("PROJCS") || /\bunits\s*=\s*m/.test(meta.crs_wkt);
  const units = wktSaysProjected ? "m" : "degrees";

  let projection;
  try {
    projection = new ol.proj.Projection({
      code: crsCode,
      units: units,
      extent: extent,
      axisOrientation: "enu",
    });
  } catch (e) {
    throw new Error(`OL Projection failed: ${e && e.message ? e.message : e}`);
  }

  // Remove any previous raster layer, then add the new one *under* the
  // vector layer so the polygon stays visible above the raster.
  if (rasterLayer) {
    map.removeLayer(rasterLayer);
  }
  rasterLayer = new ol.layer.Image({
    source: new ol.source.ImageStatic({
      url: `${BACKEND}/api/geotiff/${meta.id}/preview.png`,
      imageExtent: extent,
      projection: projection,
    }),
  });
  map.getLayers().insertAt(0, rasterLayer);

  // Switch the view to the raster's CRS. Existing features in the old CRS
  // would be meaningless now, so clear the vector layer - the polygon is
  // redrawn from scratch in the new CRS.
  currentCrsCode = crsCode;
  vectorSource.clear();

  const center = [
    (extent[0] + extent[2]) / 2,
    (extent[1] + extent[3]) / 2,
  ];
  const width = Math.abs(extent[2] - extent[0]);
  const height = Math.abs(extent[3] - extent[1]);
  const span = Math.max(width, height) || 1;
  // Rough resolution: fit the longer dimension into ~800 px.
  const resolution = span / 800;

  map.setView(
    new ol.View({
      projection: projection,
      center: center,
      resolution: resolution,
      constrainResolution: false,
    })
  );
}

// ---- Boot -----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initMap();
  if (fileInput) {
    fileInput.addEventListener("change", handleFileUpload);
  }
});
