"""Config and options flow."""

import aiohttp
import pytest
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.frigate_panel.const import (
    CONF_ALLOW_DIRECT,
    CONF_CACHE_SIZE_MB,
    CONF_DIRECT_URL,
    CONF_EVENT_MEDIA_TTL,
    CONF_SIDEBAR_TITLE,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    PANEL_URL_PATH,
)

from .conftest import FRIGATE_URL


async def start(hass):
    """Begin the user flow."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_successful_setup(hass, aioclient_mock):
    """The happy path stores the URL and seeds the direct URL from it."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/version", text="0.16.0")

    result = await hass.config_entries.flow.async_configure(
        (await start(hass))["flow_id"],
        {CONF_URL: FRIGATE_URL, CONF_VERIFY_SSL: True},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: FRIGATE_URL, CONF_VERIFY_SSL: True}
    assert result["options"][CONF_DIRECT_URL] == FRIGATE_URL


async def test_trailing_slash_is_stripped(hass, aioclient_mock):
    """A trailing slash would otherwise produce doubled slashes upstream."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/version", text="0.16.0")

    result = await hass.config_entries.flow.async_configure(
        (await start(hass))["flow_id"],
        {CONF_URL: f"{FRIGATE_URL}/", CONF_VERIFY_SSL: True},
    )
    assert result["data"][CONF_URL] == FRIGATE_URL


async def test_frigate_with_its_own_auth_is_accepted(hass, aioclient_mock):
    """A 401 means Frigate is there; its login page shows inside the panel."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/version", status=401)

    result = await hass.config_entries.flow.async_configure(
        (await start(hass))["flow_id"],
        {CONF_URL: FRIGATE_URL, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unreachable_frigate_is_reported(hass, aioclient_mock):
    """The user should learn at setup time, not from a blank panel."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/version", exc=aiohttp.ClientError)

    result = await hass.config_entries.flow.async_configure(
        (await start(hass))["flow_id"],
        {CONF_URL: FRIGATE_URL, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("bad", ["frigate.test:5000", "not a url", "ftp://host/", ""])
async def test_url_must_have_a_scheme(hass, bad):
    """A bare host cannot be proxied."""
    result = await hass.config_entries.flow.async_configure(
        (await start(hass))["flow_id"], {CONF_URL: bad, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_only_one_instance(hass, setup_entry):
    """One sidebar entry, one proxy prefix, one cache."""
    result = await start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_round_trip(hass, setup_entry):
    """Options are stored and the entry reloads with them."""
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SIDEBAR_TITLE: "Cameras",
            CONF_ALLOW_DIRECT: False,
            CONF_DIRECT_URL: "http://192.168.1.10:5000/",
            CONF_CACHE_SIZE_MB: 128,
            CONF_EVENT_MEDIA_TTL: 900,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options[CONF_SIDEBAR_TITLE] == "Cameras"
    assert setup_entry.options[CONF_ALLOW_DIRECT] is False
    # Normalised on the way in, like the setup URL.
    assert setup_entry.options[CONF_DIRECT_URL] == "http://192.168.1.10:5000"
    assert setup_entry.options[CONF_EVENT_MEDIA_TTL] == 900


async def test_options_reject_a_bad_direct_url(hass, setup_entry):
    """A malformed direct URL would render a blank panel."""
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DIRECT_URL: "192.168.1.10"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DIRECT_URL: "invalid_url"}


async def test_options_change_the_sidebar_title(hass, setup_entry):
    """Renaming should be visible in the sidebar without a restart."""
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SIDEBAR_TITLE: "Cameras"}
    )
    await hass.async_block_till_done()

    assert hass.data[DATA_PANELS][PANEL_URL_PATH].sidebar_title == "Cameras"
