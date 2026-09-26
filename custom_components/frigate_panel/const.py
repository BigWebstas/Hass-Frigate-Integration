"""Constants for the Frigate Panel integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "frigate_panel"

# Config entry keys.
CONF_URL: Final = "url"
CONF_VERIFY_SSL: Final = "verify_ssl"

# Options keys.
CONF_DIRECT_URL: Final = "direct_url"
CONF_ALLOW_DIRECT: Final = "allow_direct"
CONF_CACHE_SIZE_MB: Final = "cache_size_mb"
CONF_EVENT_MEDIA_TTL: Final = "event_media_ttl"
CONF_SIDEBAR_TITLE: Final = "sidebar_title"

DEFAULT_ALLOW_DIRECT: Final = True
DEFAULT_CACHE_SIZE_MB: Final = 512
DEFAULT_EVENT_MEDIA_TTL: Final = 600
DEFAULT_SIDEBAR_TITLE: Final = "Frigate"

# Sidebar / frontend. Bump PANEL_JS_VERSION whenever frigate-panel.js changes
# so browsers do not keep serving a stale module from cache.
PANEL_JS_VERSION: Final = "1"
PANEL_URL_PATH: Final = "frigate-panel"
PANEL_ICON: Final = "mdi:cctv"
PANEL_WEBCOMPONENT: Final = "frigate-panel"
STATIC_PATH: Final = "/frigate_panel_static"

# Proxy routing. The panel iframe and every Frigate sub-resource live under
# this prefix, which is also what we advertise to Frigate as X-Ingress-Path so
# it rebases its own asset URLs.
PROXY_BASE: Final = "/api/frigate_panel"

# Session cookie used to authenticate iframe sub-resource requests, which
# cannot carry an Authorization header. See session.py for the rationale.
COOKIE_NAME: Final = "frigate_panel_session"
SESSION_TTL: Final = 24 * 60 * 60
MAX_SESSIONS: Final = 512

# Largest response we are willing to hold in the disk cache. Anything bigger
# (recordings, exports) is streamed straight through instead.
MAX_ENTRY_BYTES: Final = 16 * 1024 * 1024
