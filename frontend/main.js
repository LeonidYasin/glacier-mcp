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
  ws.onmessage = async (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "polygons") {
      applyCollectionFromServer(msg);
    } else if (msg.type === "gee_basemap") {
      // Server (agent via the gee_get_basemap MCP tool) resolved an XYZ
      // tile URL for a Sentinel-2 scene and pushed it here. Swap the
      // layer and, when the server sent a bbox, recenter the view so the
      // user actually sees the scene instead of an empty ocean.
      applyGeeBasemapFromServer(msg);
    } else if (msg.type === "capture_map_request") {
      // Server (agent via MCP tool) asks us to screenshot the map.
      // Reply with the same request_id so the server can match it.
      try {
        const result = await captureMap(msg.options || {});
        ws.send(
          JSON.stringify({
            type: "capture_map_response",
            request_id: msg.request_id,
            ok: true,
            ...result,
          })
        );
      } catch (err) {
        ws.send(
          JSON.stringify({
            type: "capture_map_response",
            request_id: msg.request_id,
            ok: false,
            error: String(err && err.message ? err.message : err),
          })
        );
      }
    }
  };
  ws.onclose = () => {
    setTimeout(connectWebSocket, 1000);
  };
}

// ---- Map screenshot (used by the agent via the capture_map MCP tool) -----
//
// Renders the map element with html2canvas, downscales to a thumbnail, and
// returns both the full-size PNG (base64) and the thumbnail. The server saves
// the full image to disk and passes the thumbnail to the MCP client.
//
// options:
//   fullViewport (bool, default false) — include the whole page body, not just
//                                       the #map element.
//   thumbWidth   (int,  default 512)   — thumbnail width in pixels.
async function captureMap(options) {
  const fullViewport = !!options.fullViewport;
  const thumbWidth = options.thumbWidth || 512;
  const target = fullViewport ? document.body : document.getElementById("map");
  if (!target) {
    throw new Error("capture target not found");
  }
  if (typeof html2canvas !== "function") {
    throw new Error("html2canvas not loaded");
  }
  const canvas = await html2canvas(target, {
    useCORS: true,
    allowTaint: false,
    logging: false,
    backgroundColor: "#000",
    scale: 1,
  });
  const fullDataUrl = canvas.toDataURL("image/png");
  const fullBase64 = fullDataUrl.replace(/^data:image\/png;base64,/, "");
  // Downscale thumbnail in a second canvas (browser does the resampling).
  const ratio = canvas.height / canvas.width;
  const thumbH = Math.round(thumbWidth * ratio);
  const thumbCanvas = document.createElement("canvas");
  thumbCanvas.width = thumbWidth;
  thumbCanvas.height = thumbH;
  const ctx = thumbCanvas.getContext("2d");
  ctx.drawImage(canvas, 0, 0, thumbWidth, thumbH);
  const thumbDataUrl = thumbCanvas.toDataURL("image/png");
  const thumbBase64 = thumbDataUrl.replace(/^data:image\/png;base64,/, "");
  return {
    full_base64: fullBase64,
    thumb_base64: thumbBase64,
    width: canvas.width,
    height: canvas.height,
    thumb_width: thumbWidth,
    thumb_height: thumbH,
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
    // Default OL controls (zoom buttons, attribution) plus a metric scale
    // line in the bottom-left corner. The scale line is redrawn on every
    // zoom; its pixel length and label follow the current view resolution,
    // so it shows how many metres on the ground one screen-length covers.
    // This is what makes the bar independent of the physical screen DPI —
    // unlike the "1 cm = X m" text in the HUD, which assumes a 96-dpi
    // reference display.
    controls: ol.control.defaults.defaults().extend([
      new ol.control.ScaleLine({
        units: "metric",
        bar: false,
        steps: 4,
        text: false,
        minWidth: 100,
        maxWidth: 200,
      }),
    ]),
  });

  // ---- Draggable scale line -------------------------------------------
  // OL's ScaleLine control is anchored to a fixed corner via CSS. We let
  // the user pick it up and drop it anywhere over the map, so it can be
  // laid next to a feature to read off a linear size — the same gesture
  // QGIS / ArcGIS users expect. Position is NOT persisted: a page reload
  // always snaps the bar back to its CSS default (bottom-centre, flush
  // with the polygon panel).
  makeScaleLineDraggable();

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

// ---- Draggable scale line ------------------------------------------------
//
// OL's ScaleLine control renders as a <div class="ol-scale-line"> that CSS
// anchors to the bottom-centre of the map (flush with the polygon panel).
// We let the user pick it up and drop it anywhere over the map, so it can be
// laid next to a feature to read off a linear size — the gesture QGIS /
// ArcGIS users expect.
//
// Position is deliberately NOT persisted: reloading the page drops the
// inline left/bottom styles and the bar snaps back to the CSS default. This
// keeps the default predictable and avoids surprising the user after F5.
function makeScaleLineDraggable() {
  if (!map) return;
  const viewport = map.getViewport();
  const el = viewport.querySelector(".ol-scale-line");
  if (!el) return;

  let dragging = false;
  let grabDx = 0; // cursor offset inside the element, x
  let grabDy = 0; // cursor offset inside the element, y
  // We remember which DragPan interactions were active before the drag so
  // we can restore exactly that state on release (some maps may keep it
  // disabled intentionally).
  let disabledDragPans = [];

  const setDragPanActive = (active) => {
    map.getInteractions().forEach((interaction) => {
      if (interaction instanceof ol.interaction.DragPan) {
        interaction.setActive(active);
      }
    });
  };

  const onPointerDown = (evt) => {
    if (evt.button !== 0) return; // left button only
    // ROOT CAUSE of "the map pans instead of the bar moving": OL attaches
    // its own pointerdown listener on the map VIEWPORT and starts a pan
    // there. Since capture phase runs top-down, that listener fires before
    // ours no matter where we attach. The reliable fix used by OL's own
    // draggable controls (ZoomSlider, Rotate) is to disable the DragPan
    // interaction for the duration of the gesture — then there is simply
    // no competing handler and our pointermove drives the bar.
    disabledDragPans = [];
    map.getInteractions().forEach((interaction) => {
      if (
        interaction instanceof ol.interaction.DragPan &&
        interaction.getActive()
      ) {
        disabledDragPans.push(interaction);
      }
    });
    setDragPanActive(false);

    evt.preventDefault();
    evt.stopPropagation();
    dragging = true;
    const rect = el.getBoundingClientRect();
    grabDx = evt.clientX - rect.left;
    grabDy = evt.clientY - rect.top;
    el.classList.add("ol-scale-line-dragging");
    try {
      el.setPointerCapture(evt.pointerId);
    } catch (_) {
      /* pointer capture is best-effort */
    }
    // Fallback listeners on document in case pointer capture is unavailable
    // in this browser — the drag still works while the button is held.
    document.addEventListener("pointermove", onPointerMove, true);
    document.addEventListener("pointerup", endDrag, true);
    document.addEventListener("pointercancel", endDrag, true);
  };

  const onPointerMove = (evt) => {
    if (!dragging) return;
    evt.preventDefault();
    evt.stopPropagation();
    const vpRect = viewport.getBoundingClientRect();
    const elRect = el.getBoundingClientRect();
    // Position of the element's top-left corner relative to the map.
    let leftPx = evt.clientX - grabDx - vpRect.left;
    let topPx = evt.clientY - grabDy - vpRect.top;
    // Clamp so the whole bar stays inside the map viewport.
    const maxLeft = Math.max(0, vpRect.width - elRect.width);
    const maxTop = Math.max(0, vpRect.height - elRect.height);
    leftPx = Math.max(0, Math.min(leftPx, maxLeft));
    topPx = Math.max(0, Math.min(topPx, maxTop));
    // CSS positions the bar via `bottom`; convert from top-relative pixels.
    const bottomPx = vpRect.height - topPx - elRect.height;
    el.style.left = leftPx + "px";
    el.style.bottom = bottomPx + "px";
    el.style.transform = "none";
  };

  const endDrag = (evt) => {
    if (!dragging) return;
    evt.stopPropagation();
    dragging = false;
    el.classList.remove("ol-scale-line-dragging");
    // Restore DragPan to whatever it was before the gesture started.
    disabledDragPans.forEach((interaction) => interaction.setActive(true));
    disabledDragPans = [];
    document.removeEventListener("pointermove", onPointerMove, true);
    document.removeEventListener("pointerup", endDrag, true);
    document.removeEventListener("pointercancel", endDrag, true);
    try {
      el.releasePointerCapture(evt.pointerId);
    } catch (_) {
      /* pointer may already be released */
    }
  };

  el.addEventListener("pointerdown", onPointerDown);
}

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
  // The number of zoom levels is bounded by the raster's own size, not by
  // an arbitrary constant. Each extra level doubles the linear pixel
  // stretch factor: at z the average source block for a tile is
  // (maxPx / 2^z) pixels wide. Once that falls below ~TILE_SIZE pixels
  // the tiles are pure upscaling — nothing to gain, and the bilinear
  // resampling begins to show visible seams between neighbouring tiles.
  // Restricting to the level where 1 tile pixel ≈ 1 source pixel keeps
  // the raster sharp and avoids pointless network traffic.
  const maxPx = Math.max(width, height) || 1;
  const MAX_Z = Math.max(0, Math.ceil(Math.log2(maxPx / TILE_SIZE)));
  const maxSpan = maxPx;
  const resolutions = [];
  for (let z = 0; z <= MAX_Z; z++) {
    resolutions.push(maxSpan / (Math.pow(2, z) * TILE_SIZE));
  }
  // ---- Raster layer: ol.source.ImageStatic ------------------------------
  //
  // This is the simplest possible raster layer: the backend renders the
  // whole source GeoTIFF once into a single PNG (cached on upload), and
  // OpenLayers just displays it at the raster's CRS bounds. No tiles, no
  // 256x256 grid, no per-viewport re-requests, no ImageCanvas. This is the
  // approach that worked before we tried to move to tiles.
  //
  // Trade-offs (accepted for a single-scene viewer):
  //   * The full raster is uploaded once, as one PNG. For a Sentinel-2
  //     scene the preview is a few megabytes — acceptable for a local
  //     single-user tool.
  //   * On zoom-in, the browser upscales that same PNG. We opt into
  //     `image-rendering: pixelated` in style.css so the upscaled pixels
  //     stay sharp (each source pixel becomes a solid NxN square) — this
  //     matches what QGIS / ArcGIS show past the native resolution.
  //   * Panning does not re-fetch anything — the whole PNG is in memory.
  //
  // The backend caches the preview PNG at upload time, so this request is
  // cheap and only fires once per uploaded raster.
  if (rasterLayer) map.removeLayer(rasterLayer);
  const previewUrl = `${BACKEND}/api/geotiff/${meta.id}/preview.png`;
  const rasterImageSource = new ol.source.ImageStatic({
    url: previewUrl,
    // The PNG covers exactly the raster's footprint in its own CRS — the
    // same `extent` we already use for the layer and for view.fit().
    imageExtent: extent,
    // Same projection as the custom CRS we registered for this raster, so
    // OL does not try to reproject the image.
    projection,
  });
  rasterLayer = new ol.layer.Image({
    // Clip the layer to the raster's own extent so OL never paints the
    // image outside its bounds.
    extent,
    source: rasterImageSource,
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
    // Keep the raster's full detail available: allow zooming FAR past the
    // raster's native pixel resolution. We deliberately do NOT put an
    // artificial floor here — the user can decide how deep to go (past the
    // native resolution the pixels just become bigger squares, and the
    // `image-rendering: pixelated` CSS keeps them crisp instead of blurry).
    //
    // The only real limit is `resolutions[0] / 1e6` — three orders of
    // magnitude below the coarsest tile resolution. Previously we used
    // `width / 100000`, which for a Sentinel-2 scene (~54 km wide) worked
    // out to ~0.55 m/px and effectively hard-stopped the wheel around 8x.
    minResolution: resolutions[0] / 1e6,
  });
  map.setView(view);
  // Re-subscribe the HUD to the NEW view. `installHud()` subscribed to the
  // original view created in `initMap()`; `map.setView(view)` throws that
  // view away, so without this the HUD freezes at whatever zoom/resolution
  // the original view had (typically zoom 2.12, resolution 3.594e+4).
  if (typeof rebindHudView === "function") {
    rebindHudView();
  }

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
let _lastResolutions = null;

const hudEls = {
  zoom: document.getElementById("hud-zoom"),
  resolution: document.getElementById("hud-resolution"),
  scale: document.getElementById("hud-scale"),
  center: document.getElementById("hud-center"),
  resLimits: document.getElementById("hud-res-limits"),
  resArray: document.getElementById("hud-res-array"),
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
  // Scale denominator in GIS notation. A CSS pixel is 1/96 inch, i.e.
  // 0.0254/96 metres on a 96-dpi reference display. resolution is metres
  // per screen pixel, so the classical "scale denominator" (the N in
  // 1:N, i.e. how many metres on the ground equals 1 metre on the map)
  // is resolution / (0.0254/96) = resolution * 96 / 0.0254. We show it as
  // "1 cm = X m", which is the more intuitive form: how many metres does
  // one centimetre on the screen represent.
  if (hudEls.scale && res !== undefined && res > 0) {
    const scaleDenom = (res * 96) / 0.0254; // 1 : scaleDenom
    const metresPerCm = scaleDenom / 100;   // 1 cm = metresPerCm m
    hudEls.scale.textContent = `1 cm = ${metresPerCm.toFixed(1)} m`;
  } else if (hudEls.scale) {
    hudEls.scale.textContent = "—";
  }
  if (hudEls.resLimits) {
    const maxR = view.getMaxResolution();
    const minR = view.getMinResolution();
    hudEls.resLimits.textContent =
      `${maxR === undefined ? "—" : maxR.toExponential(3)} / ` +
      `${minR === undefined ? "—" : minR.toExponential(3)}`;
  }
  if (hudEls.resArray && _lastResolutions) {
    hudEls.resArray.textContent = _lastResolutions
      .slice(0, 3)
      .map((v) => v.toFixed(2))
      .join(", ");
  }
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

let _hudBoundView = null;

function rebindHudView() {
  if (!map || !hudEls.zoom) return;
  const view = map.getView();
  if (_hudBoundView === view) {
    updateHudView();
    return;
  }
  if (_hudBoundView) {
    _hudBoundView.un("change:resolution", updateHudView);
    _hudBoundView.un("change:center", updateHudView);
  }
  view.on("change:resolution", updateHudView);
  view.on("change:center", updateHudView);
  _hudBoundView = view;
  updateHudView();
}

function installHud() {
  if (!map || !hudEls.zoom) return;
  rebindHudView();
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

// ---- Google Earth Engine auth ---------------------------------------------
//
// The sign-in itself happens on google.com: the server builds the consent
// URL from the app's OAuth client_id, redirects the browser there, and
// Google redirects back to /api/gee/auth/callback — which sets the
// "glacier_session" cookie and bounces us home.
//
// This module only wires the toolbar buttons and keeps the "gee-status"
// label in sync by polling /api/gee/auth/status. OAuth tokens never touch
// the browser — they live in the server's in-memory session store.

async function refreshGeeStatus() {
  const statusEl = document.getElementById("gee-status");
  const loginBtn = document.getElementById("btn-gee-login");
  const logoutBtn = document.getElementById("btn-gee-logout");
  if (!statusEl || !loginBtn || !logoutBtn) return;
  try {
    const r = await fetch("/api/gee/auth/status", { cache: "no-store" });
    if (!r.ok) return;
    const s = await r.json();
    if (s.logged_in) {
      statusEl.textContent = `GEE: ${s.email || "signed in"}`;
      statusEl.classList.add("gee-ok");
      loginBtn.hidden = true;
      logoutBtn.hidden = false;
    } else {
      statusEl.textContent = "GEE: not signed in";
      statusEl.classList.remove("gee-ok");
      loginBtn.hidden = false;
      logoutBtn.hidden = true;
    }
  } catch (err) {
    // Backend not up yet, or user is offline: leave the label as-is.
    console.warn("gee status check failed", err);
  }
}

function initGeeAuth() {
  const loginBtn = document.getElementById("btn-gee-login");
  const logoutBtn = document.getElementById("btn-gee-logout");
  if (loginBtn) {
    loginBtn.addEventListener("click", () => {
      // Full-page redirect. The server replies 302 -> accounts.google.com;
      // after consent, Google -> /api/gee/auth/callback -> "/" (this page),
      // now with a glacier_session cookie so /status reports logged_in=true.
      window.location.href = "/api/gee/auth/start";
    });
  }
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      try {
        await fetch("/api/gee/auth/logout", { method: "POST" });
      } catch (err) {
        console.warn("gee logout failed", err);
      }
      await refreshGeeStatus();
    });
  }
  // Initial check + light polling. 5 s is cheap on localhost and means
  // the label flips almost immediately after the callback redirect.
  refreshGeeStatus();
  setInterval(refreshGeeStatus, 5000);
}

// ---- Sidebar collapse -----------------------------------------------------
//
// The tool panel is a collapsible left sidebar (see index.html). Its state
// is a single class on <body> — 'sidebar-collapsed' — so both the sidebar
// and the map position react to it via CSS. We remember the choice so the
// map stays wide across reloads once the user has hidden the tools.

const SIDEBAR_STATE_KEY = "glacier-mcp:sidebar-collapsed";

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  try {
    localStorage.setItem(SIDEBAR_STATE_KEY, collapsed ? "1" : "0");
  } catch (_err) {
    // localStorage can be unavailable (private mode); the toggle still works.
  }
  // OpenLayers needs a nudge after the map container changes size.
  if (map) map.updateSize();
}

function initSidebar() {
  const toggle = document.getElementById("sidebar-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
    });
  }
  // Keyboard shortcut: B toggles the sidebar (common in GIS editors).
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "b" || ev.key === "B") {
      if (ev.target && /INPUT|SELECT|TEXTAREA/.test(ev.target.tagName)) return;
      setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
    }
  });
  // Restore the previous choice.
  let collapsed = false;
  try {
    collapsed = localStorage.getItem(SIDEBAR_STATE_KEY) === "1";
  } catch (_err) {
    collapsed = false;
  }
  document.body.classList.toggle("sidebar-collapsed", collapsed);
}

// ---- GEE basemap ----------------------------------------------------------
//
// Puts a *real* Sentinel-2 image under the polygons. The server calls
// ee.Image.getMapId() and returns an XYZ tile template; we wrap it in an
// ol.source.XYZ and add it as the lowest layer so polygons stay on top.
//
// Two HTTP calls are involved:
//   GET  /api/gee/basemap/presets  — fill the dropdown (once at boot)
//   POST /api/gee/basemap          — resolve {tile_url, ...} for a scene
//
// Everything the user can tweak lives in the toolbar: the preset (true
// colour, false colour, NDSI/NDWI/NDVI indices, ...), the opacity, and
// the scene id. The scene id is auto-filled when a search result is
// picked, but the user can also paste one by hand.

let geeBasemapLayer = null;

// Server-driven counterpart to applyGeeBasemap(): the agent resolved the
// XYZ tile URL on the backend (gee_get_basemap MCP tool) and pushed it
// over the WebSocket. We do not re-request anything — we just wrap the
// URL in ol.source.XYZ and recenter on the scene bbox if one was sent.
function applyGeeBasemapFromServer(msg) {
  if (!msg || !msg.tile_url) return;
  const statusEl = document.getElementById("status");

  // Replace any previous GEE basemap — one scene at a time.
  clearGeeBasemap();

  const source = new ol.source.XYZ({
    url: msg.tile_url,
    crossOrigin: "anonymous",
    attributions: "Sentinel-2 · Google Earth Engine",
  });
  geeBasemapLayer = new ol.layer.Tile({
    source,
    opacity: 1,
    zIndex: -1,
  });
  map.addLayer(geeBasemapLayer);

  // Recenter so the user sees the scene rather than an empty ocean.
  // bbox is [west, south, east, north] in EPSG:4326.
  if (Array.isArray(msg.bbox) && msg.bbox.length === 4 && map) {
    const [west, south, east, north] = msg.bbox;
    const extent = ol.proj.transformExtent(
      [west, south, east, north],
      "EPSG:4326",
      "EPSG:3857"
    );
    map.getView().fit(extent, { padding: [60, 60, 60, 60], duration: 350 });
  }

  // Reflect the swap in the toolbar controls, if they exist.
  const sceneInput = document.getElementById("gee-basemap-scene");
  if (sceneInput && msg.scene_id) sceneInput.value = msg.scene_id;
  const presetSelect = document.getElementById("gee-basemap-preset");
  if (presetSelect && msg.preset) presetSelect.value = msg.preset;
  const clearBtn = document.getElementById("btn-gee-basemap-clear");
  if (clearBtn) clearBtn.hidden = false;

  if (statusEl) {
    statusEl.textContent = `basemap: ${msg.preset || "layer"} (${msg.scene_id || ""})`;
  }
}

async function loadGeeBasemapPresets() {
  const select = document.getElementById("gee-basemap-preset");
  if (!select) return;
  try {
    const r = await fetch("/api/gee/basemap/presets", { cache: "no-store" });
    if (!r.ok) return;
    const data = await r.json();
    // Keep the "— none —" option, append the server-provided ones.
    for (const preset of data.presets || []) {
      const opt = document.createElement("option");
      opt.value = preset.name;
      opt.textContent = preset.label || preset.name;
      select.appendChild(opt);
    }
  } catch (err) {
    console.warn("basemap presets load failed", err);
  }
}

function clearGeeBasemap() {
  if (geeBasemapLayer) {
    map.removeLayer(geeBasemapLayer);
    geeBasemapLayer = null;
  }
  const clearBtn = document.getElementById("btn-gee-basemap-clear");
  if (clearBtn) clearBtn.hidden = true;
  const applyBtn = document.getElementById("btn-gee-basemap-apply");
  if (applyBtn) applyBtn.disabled = false;
}

async function applyGeeBasemap() {
  const sceneInput = document.getElementById("gee-basemap-scene");
  const presetSelect = document.getElementById("gee-basemap-preset");
  const opacityInput = document.getElementById("gee-basemap-opacity");
  const applyBtn = document.getElementById("btn-gee-basemap-apply");
  const clearBtn = document.getElementById("btn-gee-basemap-clear");
  const statusEl = document.getElementById("status");

  const sceneId = (sceneInput?.value || "").trim();
  const preset = presetSelect?.value || "true_color";
  const opacity = opacityInput ? parseFloat(opacityInput.value) : 1;

  if (!sceneId) {
    if (statusEl) statusEl.textContent = "basemap: enter or pick a scene id first";
    return;
  }
  if (!preset) {
    // "— none —" selected: just clear whatever is on the map.
    clearGeeBasemap();
    return;
  }

  if (applyBtn) applyBtn.disabled = true;
  if (statusEl) statusEl.textContent = `basemap: loading ${preset}…`;

  try {
    const r = await fetch("/api/gee/basemap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene_id: sceneId, preset }),
    });
    const data = await r.json();
    if (!r.ok || !data.tile_url) {
      const msg = data.error || `HTTP ${r.status}`;
      if (statusEl) statusEl.textContent = `basemap error: ${msg}`;
      console.warn("basemap request failed", data);
      return;
    }

    // Replace any previous GEE basemap — one scene at a time keeps the
    // layer stack simple and matches the single-scene tile URL model.
    clearGeeBasemap();

    const source = new ol.source.XYZ({
      url: data.tile_url,
      crossOrigin: "anonymous",
      // The Earth Engine tile endpoint is not a standard {z}/{x}/{y}
      // template on every API version, so let OL pick the default
      // projection (EPSG:3857, which EE serves) and maxZoom.
      attributions: "Sentinel-2 · Google Earth Engine",
    });
    geeBasemapLayer = new ol.layer.Tile({
      source,
      opacity: Number.isFinite(opacity) ? opacity : 1,
      zIndex: -1, // below polygons and the raster overlay
    });
    map.addLayer(geeBasemapLayer);

    if (clearBtn) clearBtn.hidden = false;
    if (statusEl) {
      statusEl.textContent = `basemap: ${data.preset || preset} (${data.scene_id || sceneId})`;
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = `basemap error: ${err.message || err}`;
    console.warn("basemap apply failed", err);
  } finally {
    if (applyBtn) applyBtn.disabled = false;
  }
}

function initGeeBasemap() {
  const applyBtn = document.getElementById("btn-gee-basemap-apply");
  const clearBtn = document.getElementById("btn-gee-basemap-clear");
  const presetSelect = document.getElementById("gee-basemap-preset");
  const opacityInput = document.getElementById("gee-basemap-opacity");

  if (applyBtn) applyBtn.addEventListener("click", applyGeeBasemap);
  if (clearBtn) clearBtn.addEventListener("click", clearGeeBasemap);

  // Live opacity: change the layer's alpha without re-requesting tiles.
  if (opacityInput) {
    opacityInput.addEventListener("input", () => {
      if (!geeBasemapLayer) return;
      const value = parseFloat(opacityInput.value);
      geeBasemapLayer.setOpacity(Number.isFinite(value) ? value : 1);
    });
  }

  // Switching to a preset re-loads the basemap immediately if one is up.
  if (presetSelect) {
    presetSelect.addEventListener("change", () => {
      if (geeBasemapLayer && presetSelect.value) applyGeeBasemap();
    });
  }

  loadGeeBasemapPresets();
}

// ---- Boot -----------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initMap();
  wireButtons();
  installHud();
  initGeeAuth();
  initGeeBasemap();
  if (fileInput) {
    fileInput.addEventListener("change", handleFileUpload);
  }
});
