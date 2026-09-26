"""Config flow for the Frigate panel."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ALLOW_DIRECT,
    CONF_CACHE_SIZE_MB,
    CONF_DIRECT_URL,
    CONF_EVENT_MEDIA_TTL,
    CONF_SHOW_TOOLBAR,
    CONF_SIDEBAR_ICON,
    CONF_SIDEBAR_TITLE,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_ALLOW_DIRECT,
    DEFAULT_CACHE_SIZE_MB,
    DEFAULT_EVENT_MEDIA_TTL,
    DEFAULT_SHOW_TOOLBAR,
    DEFAULT_SIDEBAR_ICON,
    DEFAULT_SIDEBAR_TITLE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
    }
)


class FrigatePanelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Frigate URL and check we can reach it."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        # One sidebar entry, one proxy prefix, one cache. Supporting several
        # Frigate instances would need a per-entry panel URL, which nothing has
        # asked for yet.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalise(user_input[CONF_URL])
            if url is None:
                errors[CONF_URL] = "invalid_url"
            else:
                error = await _probe(
                    async_get_clientsession(
                        self.hass, verify_ssl=user_input[CONF_VERIFY_SSL]
                    ),
                    url,
                )
                if error:
                    errors["base"] = error
                else:
                    return self.async_create_entry(
                        title=DEFAULT_SIDEBAR_TITLE,
                        data={
                            CONF_URL: url,
                            CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                        },
                        options={CONF_DIRECT_URL: url},
                    )

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> FrigatePanelOptionsFlow:
        """Return the options flow."""
        return FrigatePanelOptionsFlow()


class FrigatePanelOptionsFlow(OptionsFlowWithReload):
    """Tune the sidebar entry, direct access and the cache."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options."""
        if user_input is not None:
            direct = (user_input.get(CONF_DIRECT_URL) or "").strip()
            if direct and _normalise(direct) is None:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(user_input),
                    errors={CONF_DIRECT_URL: "invalid_url"},
                )
            if direct:
                user_input[CONF_DIRECT_URL] = _normalise(direct)
            return self.async_create_entry(data=user_input)

        return self.async_show_form(step_id="init", data_schema=self._schema(None))

    def _schema(self, overrides: dict[str, Any] | None) -> vol.Schema:
        current = {**self.config_entry.options, **(overrides or {})}
        return vol.Schema(
            {
                vol.Optional(
                    CONF_SIDEBAR_TITLE,
                    default=current.get(CONF_SIDEBAR_TITLE, DEFAULT_SIDEBAR_TITLE),
                ): str,
                vol.Optional(
                    CONF_SIDEBAR_ICON,
                    default=current.get(CONF_SIDEBAR_ICON, DEFAULT_SIDEBAR_ICON),
                ): selector.IconSelector(),
                vol.Optional(
                    CONF_SHOW_TOOLBAR,
                    default=current.get(CONF_SHOW_TOOLBAR, DEFAULT_SHOW_TOOLBAR),
                ): bool,
                vol.Optional(
                    CONF_ALLOW_DIRECT,
                    default=current.get(CONF_ALLOW_DIRECT, DEFAULT_ALLOW_DIRECT),
                ): bool,
                vol.Optional(
                    CONF_DIRECT_URL,
                    default=current.get(
                        CONF_DIRECT_URL, self.config_entry.data[CONF_URL]
                    ),
                ): str,
                vol.Optional(
                    CONF_CACHE_SIZE_MB,
                    default=current.get(CONF_CACHE_SIZE_MB, DEFAULT_CACHE_SIZE_MB),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=20480)),
                vol.Optional(
                    CONF_EVENT_MEDIA_TTL,
                    default=current.get(CONF_EVENT_MEDIA_TTL, DEFAULT_EVENT_MEDIA_TTL),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=31536000)),
            }
        )


def _normalise(raw: str) -> str | None:
    """Return the URL without a trailing slash, or None if unusable."""
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return raw.strip().rstrip("/")


async def _probe(session: aiohttp.ClientSession, url: str) -> str | None:
    """Check Frigate answers, returning an error key if it does not."""
    try:
        async with session.get(f"{url}/api/version", allow_redirects=False) as response:
            # A 401 or 403 means Frigate is there with its own auth enabled,
            # which the panel handles by showing Frigate's login page.
            if response.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                return "cannot_connect"
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Could not reach Frigate at %s: %s", url, err)
        return "cannot_connect"
    return None
