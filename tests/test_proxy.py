"""End-to-end behaviour of the panel, its auth boundary and its cache."""

import pytest
from homeassistant.components.frontend import DATA_PANELS

from custom_components.frigate_panel.const import (
    COOKIE_NAME,
    PANEL_URL_PATH,
    PROXY_BASE,
)

from .conftest import FRIGATE_URL

CACHE_HEADER = "X-Frigate-Panel-Cache"

IMMUTABLE = {
    "Cache-Control": "public, max-age=31536000",
    "Content-Type": "application/javascript",
    "Content-Length": "14",
}
NO_STORE = {"Cache-Control": "no-store", "Content-Type": "application/json"}
NO_HEADERS_AT_ALL = {"Content-Type": "image/jpeg"}


def base(entry):
    """Proxy prefix for an entry."""
    return f"{PROXY_BASE}/{entry.entry_id}"


async def test_panel_appears_in_the_sidebar(hass, setup_entry):
    """The whole point: Frigate is in the sidebar."""
    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]
    assert panel.sidebar_title == "Frigate"
    assert panel.config["proxy_base"] == base(setup_entry)
    assert panel.config["_panel_custom"]["name"] == "frigate-panel"


async def test_panel_is_removed_on_unload(hass, setup_entry):
    """Unloading must not leave a dead sidebar entry."""
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]


async def test_unauthenticated_request_is_rejected(
    hass, setup_entry, hass_client_no_auth, aioclient_mock
):
    """Frigate must not become reachable without Home Assistant auth."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/config", text="{}", headers=NO_STORE)

    client = await hass_client_no_auth()
    response = await client.get(f"{base(setup_entry)}/api/config")

    assert response.status == 401
    assert not aioclient_mock.mock_calls


async def test_session_cookie_authorises_sub_resources(
    hass, setup_entry, hass_client, hass_client_no_auth, aioclient_mock
):
    """An iframe cannot send a bearer token, so the cookie has to carry it."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/config", text="{}", headers=NO_STORE)
    prefix = base(setup_entry)

    authed = await hass_client()
    minted = await authed.post(f"{prefix}/-/session")
    assert minted.status == 200
    cookie = minted.cookies[COOKIE_NAME]

    # Scoped to this entry's proxy prefix and not readable by scripts.
    assert cookie["path"] == prefix
    assert cookie["httponly"]
    assert cookie["samesite"].lower() == "strict"

    anon = await hass_client_no_auth()
    response = await anon.get(
        f"{prefix}/api/config", headers={"Cookie": f"{COOKIE_NAME}={cookie.value}"}
    )
    assert response.status == 200


async def test_forged_cookie_is_rejected(
    hass, setup_entry, hass_client_no_auth, aioclient_mock
):
    """A made-up session token must not work."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/config", text="{}", headers=NO_STORE)

    anon = await hass_client_no_auth()
    response = await anon.get(
        f"{base(setup_entry)}/api/config",
        headers={"Cookie": f"{COOKIE_NAME}=not-a-real-token"},
    )
    assert response.status == 401


async def test_ingress_header_is_sent_so_frigate_rebases_its_assets(
    hass, setup_entry, hass_client, aioclient_mock
):
    """Without this header the SPA requests /assets/... at Home Assistant's root."""
    aioclient_mock.get(f"{FRIGATE_URL}/", text="<html></html>", headers=NO_STORE)

    client = await hass_client()
    await client.get(f"{base(setup_entry)}/")

    headers = aioclient_mock.mock_calls[0][3]
    assert headers["X-Ingress-Path"] == base(setup_entry)


async def test_home_assistant_token_is_not_forwarded_to_frigate(
    hass, setup_entry, hass_client, aioclient_mock
):
    """A Home Assistant credential has no business reaching Frigate."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/config", text="{}", headers=NO_STORE)

    client = await hass_client()
    await client.get(f"{base(setup_entry)}/api/config")

    headers = aioclient_mock.mock_calls[0][3]
    assert "Authorization" not in headers


async def test_immutable_asset_is_cached_and_served_from_disk(
    hass, setup_entry, hass_client, aioclient_mock
):
    """Second load of the UI bundle must not touch Frigate."""
    url = f"{FRIGATE_URL}/assets/index-a1b2c3.js"
    aioclient_mock.get(url, text="console.log(1)", headers=IMMUTABLE)
    client = await hass_client()
    path = f"{base(setup_entry)}/assets/index-a1b2c3.js"

    first = await client.get(path)
    assert first.status == 200
    assert first.headers[CACHE_HEADER] == "miss"
    assert await first.text() == "console.log(1)"

    second = await client.get(path)
    assert second.status == 200
    assert second.headers[CACHE_HEADER] == "hit"
    assert await second.text() == "console.log(1)"

    assert len(aioclient_mock.mock_calls) == 1


async def test_event_thumbnail_is_cached_despite_frigate_sending_no_headers(
    hass, setup_entry, hass_client, aioclient_mock
):
    """This is the review-list win: Frigate declares nothing for event images."""
    url = f"{FRIGATE_URL}/api/events/1700000000.1-abc/thumbnail.jpg"
    aioclient_mock.get(url, content=b"\xff\xd8jpeg", headers=NO_HEADERS_AT_ALL)
    client = await hass_client()
    path = f"{base(setup_entry)}/api/events/1700000000.1-abc/thumbnail.jpg"

    first = await client.get(path)
    assert first.headers[CACHE_HEADER] == "miss"
    # We supply the cache header Frigate omits, so the browser stops re-asking.
    assert first.headers["Cache-Control"] == "public, max-age=600"

    second = await client.get(path)
    assert second.headers[CACHE_HEADER] == "hit"
    assert await second.read() == b"\xff\xd8jpeg"

    assert len(aioclient_mock.mock_calls) == 1


async def test_no_store_response_is_never_cached(
    hass, setup_entry, hass_client, aioclient_mock
):
    """Frigate marks API JSON no-store because it changes; obey that."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/config", text="{}", headers=NO_STORE)
    client = await hass_client()
    path = f"{base(setup_entry)}/api/config"

    first = await client.get(path)
    assert first.headers[CACHE_HEADER] == "bypass"
    await client.get(path)

    assert len(aioclient_mock.mock_calls) == 2


async def test_live_image_is_never_cached(
    hass, setup_entry, hass_client, aioclient_mock
):
    """latest.jpg is a live frame; serving a stale one would be wrong."""
    url = f"{FRIGATE_URL}/api/front_door/latest.jpg"
    aioclient_mock.get(url, content=b"frame", headers=NO_HEADERS_AT_ALL)
    client = await hass_client()
    path = f"{base(setup_entry)}/api/front_door/latest.jpg"

    first = await client.get(path)
    assert first.status == 200
    assert CACHE_HEADER not in first.headers
    await client.get(path)

    assert len(aioclient_mock.mock_calls) == 2


async def test_range_request_bypasses_the_cache(
    hass, setup_entry, hass_client, aioclient_mock
):
    """Caching a 206 as if it were the whole body would break mp4 seeking."""
    url = f"{FRIGATE_URL}/api/events/1700000000.1-abc/clip.mp4"
    aioclient_mock.get(url, content=b"partial", headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    path = f"{base(setup_entry)}/api/events/1700000000.1-abc/clip.mp4"

    response = await client.get(path, headers={"Range": "bytes=0-6"})
    assert response.status == 200
    assert CACHE_HEADER not in response.headers

    await client.get(path, headers={"Range": "bytes=0-6"})
    assert len(aioclient_mock.mock_calls) == 2


async def test_query_string_varies_the_cache_entry(
    hass, setup_entry, hass_client, aioclient_mock
):
    """A cropped snapshot must not be served for an uncropped request."""
    url = f"{FRIGATE_URL}/api/events/1700000000.1-abc/snapshot.jpg"
    aioclient_mock.get(url, content=b"snap", headers=NO_HEADERS_AT_ALL)
    client = await hass_client()
    path = f"{base(setup_entry)}/api/events/1700000000.1-abc/snapshot.jpg"

    await client.get(f"{path}?bbox=1")
    await client.get(f"{path}?bbox=0")

    assert len(aioclient_mock.mock_calls) == 2


async def test_post_is_proxied_but_not_cached(
    hass, setup_entry, hass_client, aioclient_mock
):
    """The UI writes as well as reads -- sub labels, config saves, WebRTC offers."""
    url = f"{FRIGATE_URL}/api/events/1700000000.1-abc/sub_label"
    aioclient_mock.post(url, text='{"success":true}')
    client = await hass_client()

    response = await client.post(
        f"{base(setup_entry)}/api/events/1700000000.1-abc/sub_label",
        json={"subLabel": "postman"},
    )
    assert response.status == 200
    assert len(aioclient_mock.mock_calls) == 1


async def test_upstream_failure_becomes_a_bad_gateway(
    hass, setup_entry, hass_client, aioclient_mock
):
    """A Frigate that is down should not surface as a 500."""
    aioclient_mock.get(f"{FRIGATE_URL}/api/config", exc=TimeoutError)

    client = await hass_client()
    response = await client.get(f"{base(setup_entry)}/api/config")
    assert response.status in (502, 504)


async def test_cache_survives_a_reload(hass, setup_entry, hass_client, aioclient_mock):
    """Restarting or reloading must not throw away a warm cache."""
    url = f"{FRIGATE_URL}/assets/index-a1b2c3.js"
    aioclient_mock.get(url, text="console.log(1)", headers=IMMUTABLE)
    client = await hass_client()
    path = f"{base(setup_entry)}/assets/index-a1b2c3.js"

    await client.get(path)
    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()

    response = await client.get(path)
    assert response.headers[CACHE_HEADER] == "hit"
    assert len(aioclient_mock.mock_calls) == 1


@pytest.mark.parametrize("stale_entry_id", ["does-not-exist"])
async def test_unknown_entry_is_not_proxied(
    hass, setup_entry, hass_client, stale_entry_id
):
    """A stale URL must not reach anything."""
    client = await hass_client()
    response = await client.get(f"{PROXY_BASE}/{stale_entry_id}/api/config")
    assert response.status in (401, 404)
