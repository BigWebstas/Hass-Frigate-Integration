"""Runtime data shared between the config entry and the proxy views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .cache import DiskCache
    from .session import SessionStore


@dataclass
class PanelData:
    """Everything the proxy needs to serve one Frigate instance."""

    hass: HomeAssistant
    entry_id: str
    url: str
    verify_ssl: bool
    event_media_ttl: int
    cache: DiskCache
    sessions: SessionStore

    @property
    def namespace(self) -> str:
        """Cache namespace.

        Keyed on the entry and the upstream URL: Frigate rewrites asset bodies
        from the X-Ingress-Path we send, and repointing the entry at a
        different Frigate must not serve the old one's cached bodies.
        """
        return f"{self.entry_id}|{self.url}"
