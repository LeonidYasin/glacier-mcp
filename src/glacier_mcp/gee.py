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

#: Earth Engine scope. ``earthengine`` is the legacy scope, still required
#: by ``ee.Initialize`` for the Python API to talk to the backend.
SCOPES = [
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/userinfo.email",
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


def build_auth_url(state: str | None = None) -> str:
    """Build the Google consent URL the browser should be sent to.

    ``state`` is an opaque anti-CSRF token; the caller generates one, stores
    it in the session, and verifies it on the callback.
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
    return url


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


def exchange_code(code: str) -> UserCredentials:
    """Trade an authorization ``code`` for a refresh token + email.

    Called from the ``/api/gee/auth/callback`` handler. On success, the
    caller stores the returned object in the session and redirects the
    browser back to the UI.
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
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


def init_ee(user: UserCredentials, project: str | None = None) -> None:
    """Initialise Earth Engine with the user's credentials.

    ``ee.Initialize`` is global state; calling it again with different
    credentials switches the active user, which is exactly what we want in
    a single-user localhost app. We remember whose credentials are active
    so we can skip redundant re-initialisation.
    """
    import ee  # imported lazily: it is a heavy dependency

    if _initialized_for == (user.email or user.refresh_token):
        return

    project = project or os.environ.get("EE_PROJECT") or None
    ee.Initialize(credentials=user.to_google_credentials(), project=project)

    global _initialized_for
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
    image = ee.Image(S2_COLLECTION + "/" + scene_id).clip(region)

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
    image = ee.Image(S2_COLLECTION + "/" + scene_id).clip(region)
    vis = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000, "gamma": 1.4}
    return image.getThumbURL(
        {
            "region": region,
            "dimensions": f"{width}x{height}",
            "format": "png",
            "visParams": vis,
        }
    )
