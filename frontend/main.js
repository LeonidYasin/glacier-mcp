// glacier-mcp frontend.
//
// Minimal interactive map:
//   * OpenLayers 10 map with an OSM base layer (placeholder)
//   * Draw interaction to create a polygon
//   * Modify interaction to drag vertices
//   * Snap to vertices for precision
//   * WebSocket to the backend so mouse edits and agent edits share
//     the same polygon state.
//
// Loading a real GeoTIFF is a follow-up (see docs/architecture.md).

const STATUS_EL = document.getElementById("status");
function setStatus(text) {
  if (STATUS_EL) STATUS_EL.textContent = text;
}

// ---- WebSocket ------------------------------------------------------------

let socket = null;
let suppressBroadcast = false; // set while applying a server snapshot

function connectSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.addEventListener("open", () => setStatus("connected to backend"));
  socket.addEventListener("close", () => {
    setStatus("backend disconnected, retrying...");
    setTimeout(connectSocket, 1000);
  });
  socket.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "snapshot") applySnapshot(msg);
    else if (msg.type === "error") setStatus(`server: ${msg.message}`);
  });
}

function sendToServer(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  if (suppressBroadcast) return;
  socket.send(JSON.stringify(payload));
}

// ---- OpenLayers -----------------------------------------------------------

const baseLayer = new ol.layer.Tile({
  source: new ol.source.OSM(),
  // The real GeoTIFF will replace this in a follow-up commit.
});

const vectorSource = new ol.source.Vector();
const vectorLayer = new ol.layer.Vector({
  source: vectorSource,
  style: new ol.style.Style({
    fill: new ol.style.Fill({ color: "rgba(80, 170, 255, 0.25)" }),
    stroke: new ol.style.Stroke({ color: "#50aaff", width: 2 }),
    image: new ol.style.Circle({
      radius: 5,
      fill: new ol.style.Fill({ color: "#ffffff" }),
      stroke: new ol.style.Stroke({ color: "#1a73c7", width: 2 }),
    }),
  }),
});

const map = new ol.Map({
  target: "map",
  layers: [baseLayer, vectorLayer],
  view: new ol.View({
    center: ol.proj.fromLonLat([0, 0]),
    zoom: 2,
  }),
});

// ---- Draw -----------------------------------------------------------------

let currentFeature = null;

const draw = new ol.interaction.Draw({
  source: vectorSource,
  type: "Polygon",
});
draw.on("drawend", (evt) => {
  // Only one polygon at a time; remove any previous one.
  vectorSource.clear();
  currentFeature = evt.feature;
  const ring = currentFeature.getGeometry().getCoordinates()[0];
  sendToServer({ action: "replace", ring });
  setStatus("polygon drawn");
});
map.addInteraction(draw);

// ---- Modify + Snap --------------------------------------------------------

const modify = new ol.interaction.Modify({ source: vectorSource });
modify.on("modifyend", () => {
  if (!currentFeature) return;
  const ring = currentFeature.getGeometry().getCoordinates()[0];
  // Send the full ring; the backend treats it as a replace, which is
  // functionally equivalent to a sequence of vertex moves.
  sendToServer({ action: "replace", ring });
  setStatus("polygon modified");
});
map.addInteraction(modify);

const snap = new ol.interaction.Snap({ source: vectorSource });
map.addInteraction(snap);

// ---- Snapshot application -------------------------------------------------

function applySnapshot(msg) {
  suppressBroadcast = true;
  try {
    if (!msg.polygon) return;
    const coords = msg.polygon.geometry.coordinates;
    // GeoJSON rings are closed (first == last); OpenLayers is fine with that.
    const geom = new ol.geom.Polygon(coords);
    if (!currentFeature) {
      currentFeature = new ol.Feature({ geometry: geom });
      vectorSource.addFeature(currentFeature);
    } else {
      currentFeature.setGeometry(geom);
    }
    const n = msg.vertices ? msg.vertices.length : 0;
    setStatus(`synced from backend (${n} vertices)`);
  } finally {
    suppressBroadcast = false;
  }
}

// ---- Boot -----------------------------------------------------------------

connectSocket();
setStatus("glacier-mcp ready - draw a polygon");
console.info("glacier-mcp frontend: OpenLayers + WebSocket ready");
