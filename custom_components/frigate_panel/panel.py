"""Sidebar panel registration.

The panel is a small custom element rather than a plain iframe panel because
the choice between reaching Frigate directly and going through the proxy can
only be made in the browser -- the server cannot know whether a particular
client shares a network with Frigate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig

from .const import (
    CONF_ALLOW_DIRECT,
    CONF_DIRECT_URL,
    CONF_SIDEBAR_TITLE,
    DEFAULT_ALLOW_DIRECT,
    DEFAULT_SIDEBAR_TITLE,
    PANEL_ICON,
    PANEL_JS_VERSION,
    PANEL_URL_PATH,
    PANEL_WEBCOMPONENT,
    PROXY_BASE,
    STATIC_PATH,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_register_static(hass: HomeAssistant) -> None:
    """Serve the panel's JavaScript.

    aiohttp cannot unregister routes, so this runs once per Home Assistant
    lifetime; callers guard against repeat registration.
    """
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                STATIC_PATH,
                str(Path(__file__).parent / "frontend"),
                cache_headers=True,
            )
        ]
    )


async def async_register_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Add the Frigate entry to the sidebar."""
    options = entry.options
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT,
        sidebar_title=options.get(CONF_SIDEBAR_TITLE, DEFAULT_SIDEBAR_TITLE),
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_PATH}/frigate-panel.js?v={PANEL_JS_VERSION}",
        embed_iframe=False,
        require_admin=False,
        config={
            "proxy_base": f"{PROXY_BASE}/{entry.entry_id}",
            # Where the browser should try to reach Frigate without the proxy.
            # Falls back to the configured upstream URL, which is only useful
            # when that host is reachable from the client too.
            "direct_url": (options.get(CONF_DIRECT_URL) or "").rstrip("/"),
            "allow_direct": options.get(CONF_ALLOW_DIRECT, DEFAULT_ALLOW_DIRECT),
        },
    )


def async_remove_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar entry."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
