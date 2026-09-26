"""Shared test fixtures."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.frigate_panel.const import (
    CONF_DIRECT_URL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

FRIGATE_URL = "http://frigate.test:5000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components in tests."""
    return


@pytest.fixture
def entry():
    """A configured Frigate panel entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Frigate",
        data={CONF_URL: FRIGATE_URL, CONF_VERIFY_SSL: True},
        options={CONF_DIRECT_URL: FRIGATE_URL},
    )


@pytest.fixture
async def setup_entry(hass, entry, aioclient_mock):
    """Set the entry up and return it.

    Depends on aioclient_mock so the upstream session is already patched when
    the proxy view captures it during setup.
    """
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
