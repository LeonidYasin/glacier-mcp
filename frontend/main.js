// glacier-mcp frontend: OpenLayers map + polygon editing + WebSocket sync.
//
// Responsibilities:
//   * show the loaded GeoTIFF as a basemap (once io.py lands)
//   * let the user draw and modify the glacier polygon with the mouse
//   * POST edits to the backend so the shared PolygonState sees them
//   * listen on /ws for edits coming from the agent and re-render

const BACKEND = window.location.origin;

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

// ---- OpenLayers map -------------------------------------------------------

let map = null;
let source = null;
let draw = null;
let modify = null;

function initMap() {
  source = new ol.source.Vector();

  const layer = new ol.layer.Vector({
    source,
    style: new ol.style.Style({
      stroke: new ol.style.Stroke({ color: "#00e0ff", width: 2 }),
      fill: new ol.style.Fill({ color: "rgba(0,224,255,0.15)" }),
    }),
  });

  map = new ol.Map({
    target: "map",
    layers: [layer],
    view: new ol.View({
      // Placeholder view; commit 5 will re-project to the GeoTIFF CRS.
      center: [0, 0],
      zoom: 2,
      projection: "EPSG:3857",
    }),
  });

  draw = new ol.interaction.Draw({ source, type: "Polygon" });
  modify = new ol.interaction.Modify({ source });
  map.addInteraction(draw);
  map.addInteraction(modify);

  draw.on("drawend", (evt) => {
    source.clear();
    source.addFeature(evt.feature);
    const geometry = evt.feature.getGeometry();
    fetch(`${BACKEND}/api/polygon`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: "Feature",
        properties: {},
        geometry: new ol.format.GeoJSON().writeGeometryObject(geometry),
      }),
    });
  });

  modify.on("modifyend", () => {
    const feature = source.getFeatures()[0];
    if (!feature) return;
    fetch(`${BACKEND}/api/polygon`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: "Feature",
        properties: {},
        geometry: new ol.format.GeoJSON().writeGeometryObject(feature.getGeometry()),
      }),
    });
  });

  // Initial load.
  fetch(`${BACKEND}/api/polygon`)
    .then((r) => r.json())
    .then(applyPolygonFromServer)
    .catch(() => {});

  connectWebSocket();
}

function applyPolygonFromServer(geojson) {
  if (!geojson || !geojson.geometry) return;
  const format = new ol.format.GeoJSON();
  const feature = format.readFeature(geojson, {
    dataProjection: "EPSG:3857",
    featureProjection: "EPSG:3857",
  });
  source.clear();
  source.addFeature(feature);
}

window.addEventListener("DOMContentLoaded", initMap);
