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
ol.proj.proj4.register(proj4);

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
    vectorSource.clear();
    vectorSource.addFeature(evt.feature);
    sendPolygon(evt.feature.getGeometry());
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
    setInfo(`Upload failed: ${err.message}`);
    setStatus("error");
  } finally {
    // Allow re-selecting the same file later.
    event.target.value = "";
  }
}

function applyGeoTiffToMap(meta) {
  // 1. Register the raster's CRS with proj4 (needs WKT) and OL.
  const crsCode = `RASTER:${meta.id}`;
  proj4.defs(crsCode, meta.crs_wkt);
  ol.proj.proj4.register(proj4);

  // 2. Build an OL projection object with the bounds as its extent.
  const extent = meta.bounds; // [left, bottom, right, top]
  const projection = new ol.proj.Projection({
    code: crsCode,
    units: meta.crs_epsg && meta.crs_epsg !== 4326 ? "m" : "degrees",
    extent: extent,
    axisOrientation: "enu",
  });
  ol.proj.addProjection(projection);

  // 3. Remove any previous raster layer, then add the new one.
  if (rasterLayer) map.removeLayer(rasterLayer);
  rasterLayer = new ol.layer.Image({
    source: new ol.source.ImageStatic({
      url: `${BACKEND}/api/geotiff/${meta.id}/preview.png`,
      imageExtent: extent,
      projection: projection,
    }),
  });
  // Raster goes *under* the vector layer.
  map.getLayers().insertAt(0, rasterLayer);

  // 4. Switch the view to the raster's CRS. Existing features in the old
  //    CRS would be meaningless now, so clear the vector layer - the
  //    polygon will be redrawn from scratch in the new CRS.
  currentCrsCode = crsCode;
  vectorSource.clear();

  const center = [
    (extent[0] + extent[2]) / 2,
    (extent[1] + extent[3]) / 2,
  ];
  const widthMeters = Math.abs(extent[2] - extent[0]);
  // Rough zoom: pick a resolution that fits the raster width in ~800px.
  const resolution = widthMeters / 800;

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
