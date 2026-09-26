"""Live view plumbing: websockets and unbuffered streaming.

These run against a real aiohttp server rather than a mocked client session,
because the thing under test is the actual protocol handling -- a websocket
upgrade must reach the websocket proxy and not the caching HTTP path.
"""

import aiohttp
import pytest
from aiohttp import web
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.frigate_panel.const import (
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    PROXY_BASE,
)


@pytest.fixture
async def upstream(aiohttp_server):
    """A stand-in Frigate exposing a websocket and a chunked stream."""

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(f"echo:{msg.data}")
                if msg.data == "bye":
                    break
        return ws

    async def latest_jpg(request):
        # Chunked, with no Content-Length, like an MJPEG or live frame feed.
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)
        for index in range(3):
            await response.write(f"frame{index}".encode())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/ws", websocket)
    app.router.add_get("/live/mse/api/ws", websocket)
    app.router.add_get("/api/front_door/latest.jpg", latest_jpg)
    return await aiohttp_server(app)


@pytest.fixture
async def proxied(hass, upstream):
    """A panel entry pointing at the stand-in Frigate."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Frigate",
        data={
            CONF_URL: f"http://127.0.0.1:{upstream.port}",
            CONF_VERIFY_SSL: True,
        },
        options={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize("path", ["ws", "live/mse/api/ws"])
async def test_websocket_is_proxied(hass, proxied, hass_client, path):
    """Live view rides websockets, so an upgrade must be relayed end to end."""
    client = await hass_client()

    async with client.ws_connect(
        f"{PROXY_BASE}/{proxied.entry_id}/{path}"
    ) as websocket:
        await websocket.send_str("hello")
        assert (await websocket.receive_str()) == "echo:hello"
        await websocket.send_str("again")
        assert (await websocket.receive_str()) == "echo:again"
        await websocket.send_str("bye")


async def test_websocket_requires_authentication(hass, proxied, hass_client_no_auth):
    """An unauthenticated upgrade must not reach Frigate."""
    client = await hass_client_no_auth()
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        async with client.ws_connect(f"{PROXY_BASE}/{proxied.entry_id}/ws"):
            pass
    assert err.value.status == 401


async def test_chunked_live_response_is_relayed_whole(hass, proxied, hass_client):
    """A stream with no Content-Length must arrive complete and uncached."""
    client = await hass_client()
    path = f"{PROXY_BASE}/{proxied.entry_id}/api/front_door/latest.jpg"

    response = await client.get(path)
    assert response.status == 200
    assert await response.read() == b"frame0frame1frame2"
    assert "X-Frigate-Panel-Cache" not in response.headers
