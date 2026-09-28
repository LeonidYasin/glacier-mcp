"""Google Earth Engine integration with browser-based OAuth.

Design goal: the user signs in with their *own* Google account through a
browser redirect (like Colab), rather than sharing a service-account key.
This means every user works against their own Earth Engine quota and
permissions.

The flow, end to end:

1. The UI calls ``GET /api/gee/auth/start``.
2. We build an OAuth consent URL from ``client_id`` / ``client_secret``
   (the *application's* credentials, not the user's) and 302 the browser
   to Google.
3. The user signs in and approves access on google.com.
4. Google redirects back to ``GET /api/gee/auth/callback?code=...``.
5. We exchange the code for a refresh token and store it (in memory here,
   keyed by a session id cookie set by ``app.py``).
6. Subsequent calls build a ``google.oauth2.credentials.Credentials``
   object from the refresh token and call ``ee.Initialize``.

The client_id / client_secret are the *application's* identity with
Google, not the user's. They are read from environment variables so they
never land in git::

    GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
    GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-...

For a localhost app the ``client_secret`` is not a hard secret (it can
only be used together with the registered redirect URI), but we still
keep it out of the repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: The redirect URI must match EXACTLY what is registered in the Google
#: Cloud Console for this OAuth client. For local development that is
#: http://127.0.0.1:8765/api/gee/auth/callback
REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://127.0.0.1:8765/api/gee/auth/callback",
)

#: OAuth scopes we request from Google. The Earth Engine scope is the
#: one we actually care about; `openid` + `userinfo.email` let us read
#: the signed-in user's email; `userinfo.profile` is included even though
#: we do not use the profile data, because Google's oauth2/v2/userinfo
#: endpoint silently expands a `userinfo.email` request to include
#: `userinfo.profile`. If we do not request it explicitly,
#: google-auth-oauthlib raises 'Scope has changed from ... to ...' on
#: fetch_token, which would abort the login. Requesting it up-front
#: makes the granted scope match the requested scope exactly.
SCOPES = [
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]


def _client_config() -> dict[str, Any]:
    """Return the google-auth client config dict from the environment.

    We build this dict manually instead of loading a ``client_secrets.json``
    file so the credentials can come straight from the environment / a
    ``.env.local`` file that never enters git.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set. "
            "Create an OAuth client (Web application) in Google Cloud Console "
            "with redirect URI 'http://127.0.0.1:8765/api/gee/auth/callback', "
            "then put the two values into .env.local."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [REDIRECT_URI],
        }
    }


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def start_auth_flow(state: str) -> tuple[str, Any]:
    """Build the Google consent URL and return the live ``Flow`` object.

    IMPORTANT — PKCE: google-auth-oauthlib's ``Flow`` uses PKCE. When you
    call ``flow.authorization_url()``, the flow generates a random
    ``code_verifier`` and puts the corresponding ``code_challenge`` in the
    URL sent to Google. On the callback, ``flow.fetch_token(code=...)``
    must be called on **the same Flow instance** so that it can send that
    ``code_verifier`` back to Google's token endpoint. Creating a fresh
    Flow in the callback loses the verifier and Google replies
    ``(invalid_grant) Missing code verifier``.

    The caller is expected to keep the returned Flow alive across the
    whole OAuth round-trip (see app._gee_states).
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    url, _ = flow.authorization_url(
        access_type="offline",       # ask for a refresh token
        include_granted_scopes="true",
        prompt="consent",            # force refresh token on every login
        state=state,
    )
    return url, flow


@dataclass
class UserCredentials:
    """Everything we need to re-create a user's Google credentials later.

    We keep the raw token dict, not a live ``Credentials`` object, because
    the object is not JSON-serialisable and we want to be able to move the
    session store to disk / a database without rewriting this module.
    """

    refresh_token: str
    token: str | None = None
    token_uri: str = "https://oauth2.googleapis.com/token"
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = field(default_factory=list)
    email: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_google_credentials(self):
        """Return a ``google.oauth2.credentials.Credentials`` instance."""
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=self.token,
            refresh_token=self.refresh_token,
            token_uri=self.token_uri,
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=self.scopes,
        )


def finish_auth_flow(flow: Any, code: str) -> UserCredentials:
    """Trade an authorization ``code`` for a refresh token + email.

    Must be called with the **same** ``Flow`` object that
    ``start_auth_flow`` returned — that object carries the PKCE
    ``code_verifier`` Google expects. Creating a new Flow here would fail
    with ``(invalid_grant) Missing code verifier``.
    """
    flow.fetch_token(code=code)
    creds = flow.credentials

    email = None
    try:
        from googleapiclient.discovery import build as gbuild

        # oauth2 v2 userinfo endpoint — cheap call to learn who signed in.
        info = gbuild("oauth2", "v2", credentials=creds, cache_discovery=False)
        email = info.userinfo().get().execute().get("email")
    except Exception:  # noqa: BLE001 — email is best-effort, never fatal
        pass

    cfg = _client_config()["web"]
    return UserCredentials(
        refresh_token=creds.refresh_token or "",
        token=creds.token,
        token_uri=creds.token_uri or cfg["token_uri"],
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        scopes=list(creds.scopes or SCOPES),
        email=email,
    )


# ---------------------------------------------------------------------------
# Earth Engine
# ---------------------------------------------------------------------------

_initialized_for: str | None = None

#: The currently signed-in user. Set by ``app.gee_auth_callback`` after a
#: successful OAuth exchange, cleared by ``app.gee_auth_logout``.
#:
#: We keep a *single* global active user rather than a per-session store
#: because the MCP tools (which are the main consumers of these
#: credentials) have no HTTP request to read a session cookie from — they
#: are invoked by the agent over a completely separate transport. For a
#: single-user localhost app that is exactly the right trade-off: whoever
#: last completed the OAuth flow in the browser *is* the active user.
_active_user: UserCredentials | None = None


def set_active_user(user: UserCredentials | None) -> None:
    """Record (or clear) the currently signed-in user.

    Called from ``app.gee_auth_callback`` (with a real user) and
    ``app.gee_auth_logout`` (with ``None``). Also re-initialises Earth
    Engine so subsequent calls immediately run as the new user.
    """
    global _active_user, _initialized_for
    _active_user = user
    if user is None:
        # Drop the "already initialised for" marker so a later sign-in
        # re-runs ee.Initialize even if the email happens to repeat.
        _initialized_for = None
        return
    try:
        init_ee(user)
    except Exception:  # noqa: BLE001 — surfaced later via tool errors
        pass


def get_active_user() -> UserCredentials | None:
    """Return the currently signed-in user, or None if nobody signed in."""
    return _active_user


def init_ee(user: UserCredentials, project: str | None = None) -> None:
    """Initialise Earth Engine with the user's credentials.

    ``ee.Initialize`` is global state; calling it again with different
    credentials switches the active user, which is exactly what we want in
    a single-user localhost app. We remember whose credentials are active
    so we can skip redundant re-initialisation.
    """
    global _initialized_for  # noqa: PLW0603 — module-level cache by design

    import ee  # imported lazily: it is a heavy dependency

    if _initialized_for == (user.email or user.refresh_token):
        return

    project = project or os.environ.get("EE_PROJECT") or None
    ee.Initialize(credentials=user.to_google_credentials(), project=project)
    _initialized_for = user.email or user.refresh_token


def _require_ee() -> None:
    if _initialized_for is None:
        raise RuntimeError(
            "Earth Engine is not initialised. Sign in with Google in the UI "
            "(GET /api/gee/auth/start) first."
        )


# ---------------------------------------------------------------------------
# Sentinel-2 search
# ---------------------------------------------------------------------------

#: Sentinel-2 Level-2A (surface reflectance, harmonized). This is the
#: collection that replaced COPERNICUS/S2_SR in 2024 for new data.
S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"


def _resolve_scene_id(scene_id: str) -> str:
    """Normalise a Sentinel-2 scene id to a bare asset id.

    ``search_sentinel2`` returns fully-qualified ids of the form
    ``COPERNICUS/S2_SR_HARMONIZED/<timestamp>_<timestamp>_<tile>`` (that is
    how Earth Engine reports ``system:index``-prefixed ids through the
    ``toList().getInfo()`` path). Every other function in this module
    builds the asset path as ``S2_COLLECTION + '/' + scene_id``, so passing
    the fully-qualified id straight back in used to produce
    ``COPERNICUS/S2_SR_HARMONIZED/COPERNICUS/S2_SR_HARMONIZED/<...>`` and
    fail with ``Image.load: Image asset ... not found``.

    Strip the collection prefix if present so both call styles work:
    callers may pass either the full id from a search result, or just the
    bare suffix.
    """
    prefix = S2_COLLECTION + "/"
    if scene_id.startswith(prefix):
        return scene_id[len(prefix):]
    return scene_id


def _bbox_to_ee_geometry(bbox: list[float]) -> Any:
    """Convert ``[west, south, east, north]`` (EPSG:4326) to an ee.Geometry."""
    import ee

    west, south, east, north = bbox
    return ee.Geometry.Rectangle([west, south, east, north], proj="EPSG:4326", geodesic=False)


def search_sentinel2(
    bbox: list[float],
    date_from: str,
    date_to: str,
    cloud_max: float = 20.0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return a list of Sentinel-2 scenes intersecting ``bbox``.

    bbox: ``[west, south, east, north]`` in EPSG:4326 (lon/lat degrees).
    date_from / date_to: ``YYYY-MM-DD`` strings.
    cloud_max: maximum scene cloud cover percentage (0-100).
    limit: cap on returned scenes (Earth Engine has no LIMIT, we slice).

    Each scene is a dict with id, date, cloud_cover, and a thumbnail URL.
    """
    import ee

    _require_ee()
    region = _bbox_to_ee_geometry(bbox)
    collection = (
        ee.ImageCollection(S2_COLLECTION)
        .filterBounds(region)
        .filterDate(date_from, date_to)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud_max))
        .sort("CLOUDY_PIXEL_PERCENTAGE")
    )

    count = int(collection.size().getInfo())
    if count == 0:
        return []

    scenes = collection.limit(limit).toList(limit).getInfo() or []
    out: list[dict[str, Any]] = []
    for scene in scenes:
        props = scene.get("properties", {})
        image_id = scene.get("id") or props.get("system:index")
        out.append(
            {
                "id": image_id,
                "date": props.get("DATE_ACQUIRED") or props.get("system:index", "")[:8],
                "cloud_cover": props.get("CLOUDY_PIXEL_PERCENTAGE"),
                "satellite": props.get("SPACECRAFT_NAME"),
                "tile": props.get("MGRS_TILE"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Glacier contour extraction (NDSI threshold)
# ---------------------------------------------------------------------------


def extract_glacier_contours(
    bbox: list[float],
    scene_id: str,
    ndsi_threshold: float = 0.4,
    min_area_px: int = 50,
    simplify_m: float = 10.0,
) -> list[list[list[float]]]:
    """Extract glacier outlines from one Sentinel-2 scene using NDSI.

    NDSI = (Green - SWIR1) / (Green + SWIR1). Snow and ice are bright in
    the green band and very dark in SWIR1, so NDSI > 0.4 is the standard
    first-pass glacier / snow mask. We then vectorise the mask and return
    each polygon as a list of ``[lon, lat]`` rings (EPSG:4326).

    bbox: ``[west, south, east, north]`` in EPSG:4326.
    scene_id: a Sentinel-2 scene id from :func:`search_sentinel2`.
    ndsi_threshold: NDSI cutoff (0.4 is conservative; 0.5 is stricter).
    min_area_px: drop specks smaller than this many 10 m pixels.
    simplify_m: Douglas-Peucker tolerance in metres for the rings.
    """
    import ee

    _require_ee()
    region = _bbox_to_ee_geometry(bbox)
    image = ee.Image(S2_COLLECTION + "/" + _resolve_scene_id(scene_id)).clip(region)

    # Sentinel-2 band names in COPERNICUS/S2_SR_HARMONIZED:
    #   B3 = green (~560 nm), B11 = SWIR1 (~1610 nm)
    ndsi = image.normalizedDifference(["B3", "B11"]).rename("NDSI")
    mask = ndsi.gt(ndsi_threshold)

    # Also exclude water (NDWI > 0) so glacial lakes don't turn into ice.
    ndwi = image.normalizedDifference(["B3", "B8"]).rename("NDWI")
    not_water = ndwi.lt(0.0)

    ice = mask.And(not_water).selfMask()

    min_area_m2 = min_area_px * 10 * 10  # Sentinel-2 SR is 10 m/pixel
    vectors = ice.reduceToVectors(
        geometry=region,
        scale=10,
        geometryType="polygon",
        eightConnected=False,
        maxPixels=int(1e9),
        bestEffort=True,
        tileScale=4,
    ).filter(ee.Filter.gte("count", min_area_px))

    # Simplify geometry to drop the staircase edge of 10 m pixels.
    simplified = vectors.map(lambda f: f.setGeometry(f.geometry().simplify(simplify_m)))

    features = simplified.getInfo().get("features", [])
    rings: list[list[list[float]]] = []
    for feat in features:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        coords = geom.get("coordinates") or []
        if not coords:
            continue
        ring = coords[0]  # exterior ring
        if len(ring) >= 4:  # at least a triangle
            rings.append([[float(x), float(y)] for x, y in ring])
    return rings


# --------------------------------------------------------------------------
# RGB preview
# --------------------------------------------------------------------------


def rgb_thumbnail_url(
    bbox: list[float],
    scene_id: str,
    width: int = 512,
    height: int = 512,
) -> str:
    """Return a public thumbnail URL for one scene (true-colour RGB).

    Earth Engine serves these thumbnails without auth for a short time, so
    the browser can load the URL directly as an <img src>. This is only a
    preview — for the actual raster on the map we will add a GeoTIFF
    download endpoint in a follow-up.
    """
    import ee

    _require_ee()
    region = _bbox_to_ee_geometry(bbox)
    image = ee.Image(S2_COLLECTION + "/" + _resolve_scene_id(scene_id)).clip(region)
    vis = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000, "gamma": 1.4}
    return image.getThumbURL(
        {
            "region": region,
            "dimensions": f"{width}x{height}",
            "format": "png",
            "visParams": vis,
        }
    )


# ---------------------------------------------------------------------------
# Interactive basemap (XYZ tile URL for OpenLayers / Leaflet)
# ---------------------------------------------------------------------------

#: Named visualisation presets. Each value is a ``vis_params`` dict suitable
#: for ``Image.getMapId``:
#:   bands   — list of band names, in the order (R, G, B) or single band
#:   min/max — stretch bounds (scalar or per-band list)
#:   gamma   — display gamma (scalar or per-band list)
#:   palette — optional colour ramp for single-band layers
#:   index   — alternative to bands: compute a spectral index first
#:
#: All band names come from COPERNICUS/S2_SR_HARMONIZED (harmonized L2A):
#:   B1 coastal, B2 blue, B3 green, B4 red, B5..B7 red-edge,
#:   B8 NIR, B8A narrow NIR, B9 water vapour, B11 SWIR1, B12 SWIR2.
BASEMAP_PRESETS: dict[str, dict[str, Any]] = {
    "true_color": {
        "label": "True colour (B4/B3/B2)",
        "bands": ["B4", "B3", "B2"],
        "min": 0,
        "max": 3000,
        "gamma": 1.4,
    },
    "false_color_nir": {
        "label": "False colour NIR (B8/B4/B3) — vegetation red",
        "bands": ["B8", "B4", "B3"],
        "min": 0,
        "max": 4000,
        "gamma": 1.3,
    },
    "false_color_swir": {
        "label": "False colour SWIR (B12/B8/B4) — snow/ice cyan",
        "bands": ["B12", "B8", "B4"],
        "min": 0,
        "max": 5000,
        "gamma": 1.3,
    },
    "ndsi": {
        "label": "NDSI snow/ice index (B3-B11)/(B3+B11)",
        "index": "ndsi",
        "min": -0.2,
        "max": 0.8,
        "palette": ["#000080", "#00bfff", "#ffffff", "#ffe066", "#ff0000"],
    },
    "ndwi": {
        "label": "NDWI water index (B3-B8)/(B3+B8)",
        "index": "ndwi",
        "min": -0.5,
        "max": 0.5,
        "palette": ["#8b4513", "#ffffe0", "#00bfff", "#00008b"],
    },
    "ndvi": {
        "label": "NDVI vegetation index (B8-B4)/(B8+B4)",
        "index": "ndvi",
        "min": -0.2,
        "max": 0.9,
        "palette": ["#8b4513", "#ffffe0", "#90ee90", "#006400"],
    },
    "nir_gray": {
        "label": "Grayscale NIR (B8)",
        "bands": ["B8"],
        "min": 0,
        "max": 5000,
        "gamma": 1.2,
    },
}

#: Spectral indices computed from raw bands before visualisation.
#: name -> (band_a, band_b); value = (a - b) / (a + b).
_INDEX_FORMULAS: dict[str, tuple[str, str]] = {
    "ndsi": ("B3", "B11"),
    "ndwi": ("B3", "B8"),
    "ndvi": ("B8", "B4"),
}


def _build_visualized_image(scene_id: str, vis_params: dict[str, Any]) -> Any:
    """Return an ``ee.Image`` ready for ``getMapId`` from a preset spec.

    Two kinds of layers are supported:

    * **RGB / single-band** — ``vis_params`` has a ``bands`` list; the
      bands are forwarded straight to ``getMapId``.
    * **spectral index** — ``vis_params`` has an ``index`` key; we compute
      ``(a - b) / (a + b)`` with ``ee.Image.normalizedDifference`` and let
      the palette from ``vis_params['palette']`` colour the result.
    """
    import ee

    image = ee.Image(S2_COLLECTION + "/" + _resolve_scene_id(scene_id))

    index_name = vis_params.get("index")
    if index_name:
        if index_name not in _INDEX_FORMULAS:
            raise ValueError(
                f"Unknown spectral index '{index_name}'. "
                f"Known: {sorted(_INDEX_FORMULAS)}"
            )
        band_a, band_b = _INDEX_FORMULAS[index_name]
        return image.normalizedDifference([band_a, band_b]).rename(index_name)

    return image


def get_basemap(
    scene_id: str,
    preset: str = "true_color",
    vis_params: dict[str, Any] | None = None,
    bbox: list[float] | None = None,
    clip: bool = False,
) -> dict[str, Any]:
    """Return an XYZ tile URL for one Sentinel-2 scene.

    This is the function the UI uses to draw a *real* basemap under the
    polygons instead of an empty background. Earth Engine's ``getMapId``
    returns a short-lived map id whose tile template is a plain
    ``https://earthengine.googleapis.com/v1/.../{z}/{x}/{y}`` XYZ URL that
    OpenLayers' ``ol.source.XYZ`` can consume directly — no client-side
    Earth Engine JS API, no OAuth in the browser.

    Args:
        scene_id: a scene id from :func:`search_sentinel2`; either the
            fully-qualified id or just the bare ``<timestamp>_<tile>``
            suffix (see :func:`_resolve_scene_id`).
        preset: one of :data:`BASEMAP_PRESETS` — ``true_color``,
            ``false_color_nir``, ``false_color_swir``, ``ndsi``, ``ndwi``,
            ``ndvi``, ``nir_gray``. Ignored when ``vis_params`` is given.
        vis_params: escape hatch — a raw ``vis_params`` dict (bands / min /
            max / gamma / palette / index). When present it overrides the
            preset entirely, which is what the UI sends when the user
            tweaks the layer by hand.
        bbox: optional ``[west, south, east, north]`` in EPSG:4326 to clip
            the layer to the region of interest.
        clip: if ``True`` and ``bbox`` is given, clip the image to the bbox.

    Returns a dict with ``map_id``, ``token``, ``tile_url``, ``preset``,
    ``scene_id``, ``band_names``, ``index`` and ``expires_in``.

    The caller is expected to hand ``tile_url`` to ``ol.source.XYZ`` /
    ``L.tileLayer``. The map id is valid for about 24 hours; after that
    the UI just calls this function again.
    """
    import ee

    _require_ee()

    if vis_params is None:
        if preset not in BASEMAP_PRESETS:
            raise ValueError(
                f"Unknown basemap preset '{preset}'. "
                f"Known: {sorted(BASEMAP_PRESETS)}"
            )
        vis_params = dict(BASEMAP_PRESETS[preset])
    else:
        vis_params = dict(vis_params)

    image = _build_visualized_image(scene_id, vis_params)

    if clip and bbox is not None:
        region = _bbox_to_ee_geometry(bbox)
        image = image.clip(region)

    # Earth Engine rejects unknown keys in vis_params, so hand it only the
    # fields Image.getMapId actually understands.
    ee_vis: dict[str, Any] = {}
    for key in ("bands", "min", "max", "gamma", "palette", "opacity"):
        if vis_params.get(key) is not None:
            ee_vis[key] = vis_params[key]

    map_id = image.getMapId(ee_vis)
    tile_fetcher = map_id.get("tile_fetcher") or map_id.get("tileFetcher")
    tile_url = None
    if tile_fetcher is not None:
        # The Python client wraps the response in a small object that
        # exposes both ``url_format`` and ``urlFormat`` depending on the
        # earthengine-api version; try both.
        tile_url = (
            getattr(tile_fetcher, "url_format", None)
            or getattr(tile_fetcher, "urlFormat", None)
        )
    if not tile_url:
        # Older / mocked clients return the raw JSON dict instead of the
        # wrapper object — fall back to the documented fields.
        tile_url = (
            map_id.get("url_format")
            or map_id.get("urlFormat")
            or (map_id.get("tiles") or [None])[0]
        )
    if not tile_url:
        raise RuntimeError(
            "Earth Engine did not return a tile URL from getMapId(). "
            f"Raw response keys: {sorted(map_id)}"
        )

    return {
        "map_id": map_id.get("mapid") or map_id.get("mapId") or map_id.get("name"),
        "token": map_id.get("token"),
        "tile_url": tile_url,
        "preset": preset,
        "scene_id": _resolve_scene_id(scene_id),
        "band_names": list(vis_params.get("bands", [])),
        "index": vis_params.get("index"),
        "expires_in": 86400,
    }


def basemap_presets() -> list[dict[str, Any]]:
    """Return the catalogue of named basemap presets for the UI.

    Each entry is a dict with ``name``, ``label`` and the full
    ``vis_params`` so the frontend can render a dropdown without
    duplicating the list on the JS side.
    """
    out: list[dict[str, Any]] = []
    for name, params in BASEMAP_PRESETS.items():
        entry = {"name": name, "label": params.get("label", name)}
        entry.update({k: v for k, v in params.items() if k != "label"})
        out.append(entry)
    return out
