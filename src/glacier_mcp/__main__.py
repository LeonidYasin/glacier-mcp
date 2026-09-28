"""Entry point: ``python -m glacier_mcp``.

Starts two servers in one process:
  * HTTP + WebSocket UI on http://localhost:8765 (OpenLayers map)
  * MCP streamable-http endpoint on http://localhost:8766 (for the agent)

Both share the same PolygonState via ``server.get_state()``.
"""

from __future__ import annotations

import os
import sys
import threading

# Google OAuth adds `userinfo.profile` to the granted scope whenever we
# ask for `userinfo.email` — this is a documented quirk of the
# oauth2/v2/userinfo endpoint. google-auth-oauthlib reacts by raising
# 'Scope has changed from ... to ...' on fetch_token, which kills the
# login. OAUTHLIB_RELAX_TOKEN_SCOPE=1 tells the library to accept the
# extra scope silently. Must be set BEFORE google_auth_oauthlib is
# imported anywhere in the process.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Load environment variables BEFORE importing .app / .server, because
# glacier_mcp.gee reads GOOGLE_OAUTH_CLIENT_ID / _SECRET at import time
# (module-level constants in _client_config's caller chain). If we import
# app first, os.environ will not yet contain the secrets from .env.local
# and the OAuth endpoints will raise 'GOOGLE_OAUTH_CLIENT_ID ... not set'.
#
# Two files are loaded, in order:
#   1. .env.local — the gitignored, user-provided OAuth secrets.
#   2. .env       — optional, for CI / production overrides.
# Later files win, so .env can override .env.local if both exist.
from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

from . import __version__
from .app import run as run_ui
from .server import run as run_mcp


def main() -> None:
    if "--version" in sys.argv:
        print(f"glacier-mcp {__version__}")
        return

    print(
        f"glacier-mcp {__version__}\n"
        "Map UI:  http://127.0.0.1:8765\n"
        "MCP:     http://127.0.0.1:8766\n"
    )

    # MCP in a background thread so the UI event loop stays responsive.
    mcp_thread = threading.Thread(target=run_mcp, name="mcp", daemon=True)
    mcp_thread.start()

    run_ui()


if __name__ == "__main__":
    main()
