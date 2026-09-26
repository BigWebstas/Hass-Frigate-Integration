"""Frigate in the Home Assistant sidebar, with a server-side cache."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cache import DiskCache
from .const import (
    CONF_CACHE_SIZE_MB,
    CONF_EVENT_MEDIA_TTL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_CACHE_SIZE_MB,
    DEFAULT_EVENT_MEDIA_TTL,
    DOMAIN,
    MAX_SESSIONS,
    SESSION_TTL,
)
from .data import PanelData
from .panel import async_register_panel, async_register_static, async_remove_panel
from .proxy import FrigateProxyView, FrigateSessionView
from .session import SessionStore

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# The proxy views and the panel's static path live in the aiohttp router, which
# has no way to unregister a route. They are therefore installed once per Home
# Assistant lifetime and read from this registry, which a reload repopulates.
_REGISTRY = "registry"
_ROUTES_INSTALLED = "routes_installed"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Frigate panel entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    registry: dict[str, PanelData] = domain_data.setdefault(_REGISTRY, {})

    cache_mb = entry.options.get(CONF_CACHE_SIZE_MB, DEFAULT_CACHE_SIZE_MB)
    cache = DiskCache(
        root=Path(hass.config.path(f".storage/{DOMAIN}_cache")),
        budget_bytes=cache_mb * 1024 * 1024,
    )
    await hass.async_add_executor_job(cache.prime)

    registry[entry.entry_id] = PanelData(
        hass=hass,
        entry_id=entry.entry_id,
        url=entry.data[CONF_URL],
        verify_ssl=entry.data[CONF_VERIFY_SSL],
        event_media_ttl=entry.options.get(
            CONF_EVENT_MEDIA_TTL, DEFAULT_EVENT_MEDIA_TTL
        ),
        cache=cache,
        sessions=SessionStore(ttl=SESSION_TTL, max_sessions=MAX_SESSIONS),
    )

    if not domain_data.get(_ROUTES_INSTALLED):
        await async_register_static(hass)
        # The session view must be registered first: aiohttp matches routes in
        # registration order, and the proxy's catch-all would otherwise swallow
        # the session path.
        hass.http.register_view(FrigateSessionView(registry))
        hass.http.register_view(
            FrigateProxyView(
                async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL]),
                registry,
            )
        )
        domain_data[_ROUTES_INSTALLED] = True

    await async_register_panel(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Frigate panel entry."""
    domain_data = hass.data.get(DOMAIN, {})
    registry: dict[str, PanelData] = domain_data.get(_REGISTRY, {})

    if (data := registry.pop(entry.entry_id, None)) is not None:
        # Sessions are tied to this entry's proxy prefix; dropping them means a
        # reload cannot leave usable cookies pointing at stale config.
        data.sessions.clear()

    async_remove_panel(hass)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the cache when the entry is removed."""
    cache = DiskCache(
        root=Path(hass.config.path(f".storage/{DOMAIN}_cache")), budget_bytes=0
    )
    await hass.async_add_executor_job(cache.clear)
