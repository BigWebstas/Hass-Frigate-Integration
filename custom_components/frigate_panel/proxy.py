"""HTTP and websocket proxy views for the Frigate panel.

One catch-all view serves the whole Frigate UI. It dispatches on the actual
protocol -- a request carrying ``Upgrade: websocket`` goes to the websocket
proxy, everything else to the HTTP path -- which avoids maintaining a list of
which Frigate URLs happen to be websockets.

The HTTP path caches while it streams: bytes go to the client as they arrive
and are accumulated on the side, so caching never adds latency to a miss.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import hdrs, web
from hass_web_proxy_lib import (
    HASSWebProxyLibUnauthorizedRequestError,
    ProxiedURL,
    WebsocketProxyView,
)
from homeassistant.components.http.const import (
    KEY_HASS_REFRESH_TOKEN_ID,
    KEY_HASS_USER,
)
from homeassistant.helpers.http import KEY_AUTHENTICATED, HomeAssistantView
from homeassistant.util.ssl import get_default_context, get_default_no_verify_context
from multidict import CIMultiDict

from .cache import Stored, cache_key
from .const import COOKIE_NAME, MAX_ENTRY_BYTES, PROXY_BASE
from .rules import Policy, Tier, classify, forbids_storage, parse_cache_control

if TYPE_CHECKING:
    from .data import PanelData

_LOGGER = logging.getLogger(__name__)

_CACHE_STATUS_HEADER = "X-Frigate-Panel-Cache"
_GZIP_MIN_BYTES = 1024
_DEFAULT_TTL = 3600

# Never forwarded upstream. Authorization is a Home Assistant credential and
# has no business reaching Frigate; the rest are hop-by-hop or are rebuilt by
# the outgoing request.
_DROP_FROM_REQUEST = frozenset(
    {
        hdrs.AUTHORIZATION,
        hdrs.CONNECTION,
        hdrs.CONTENT_ENCODING,
        hdrs.CONTENT_LENGTH,
        hdrs.HOST,
        hdrs.KEEP_ALIVE,
        hdrs.SEC_WEBSOCKET_EXTENSIONS,
        hdrs.SEC_WEBSOCKET_KEY,
        hdrs.SEC_WEBSOCKET_PROTOCOL,
        hdrs.SEC_WEBSOCKET_VERSION,
        hdrs.TE,
        hdrs.TRAILER,
        hdrs.TRANSFER_ENCODING,
        hdrs.UPGRADE,
    }
)

# Hop-by-hop and content-coding headers that must not be sent onward or stored.
# CORS headers are stripped because aiohttp_cors asserts they are absent.
_DROP_FROM_RESPONSE = frozenset(
    {
        hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS,
        hdrs.ACCESS_CONTROL_ALLOW_ORIGIN,
        hdrs.ACCESS_CONTROL_EXPOSE_HEADERS,
        hdrs.CONNECTION,
        hdrs.CONTENT_ENCODING,
        hdrs.CONTENT_TYPE,
        hdrs.KEEP_ALIVE,
        hdrs.PROXY_AUTHENTICATE,
        hdrs.TRAILER,
        hdrs.TRANSFER_ENCODING,
        hdrs.UPGRADE,
    }
)


class FrigateSessionView(HomeAssistantView):
    """Mints the cookie that lets iframe sub-resources authenticate.

    This is the only endpoint the panel calls with a bearer token; every
    request after it rides the cookie.
    """

    url = PROXY_BASE + "/{entry_id}/-/session"
    name = "api:frigate_panel:session"
    requires_auth = True

    def __init__(self, registry: dict[str, PanelData]) -> None:
        """Initialise with the per-entry runtime data registry."""
        self._registry = registry

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        """Issue a panel session cookie for the calling user."""
        data = self._registry.get(entry_id)
        if data is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)

        user = request.get(KEY_HASS_USER)
        refresh_token_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
        if user is None or refresh_token_id is None:
            return web.Response(status=HTTPStatus.UNAUTHORIZED)

        token, ttl = data.sessions.mint(user.id, refresh_token_id)
        response = web.json_response({"ttl": ttl})
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=ttl,
            path=f"{PROXY_BASE}/{entry_id}",
            httponly=True,
            samesite="Strict",
            secure=request.url.scheme == "https",
        )
        return response


class FrigateProxyView(WebsocketProxyView):
    """Proxies the Frigate UI, caching what is safe to cache."""

    url = PROXY_BASE + "/{entry_id}"
    extra_urls = [PROXY_BASE + "/{entry_id}/{path:.*}"]
    name = "api:frigate_panel:proxy"

    def __init__(
        self,
        websession: aiohttp.ClientSession,
        registry: dict[str, PanelData],
    ) -> None:
        """Initialise with the per-entry runtime data registry."""
        super().__init__(websession)
        self._registry = registry

    # HomeAssistantView routes only the methods a view defines, and the Frigate
    # UI uses more than GET (deleting events, saving config, posting WebRTC
    # offers). They all funnel into the same handler.
    async def get(self, request: web.Request, **kwargs: Any) -> web.StreamResponse:
        """Handle a GET."""
        return await self._guarded(request, **kwargs)

    async def post(self, request: web.Request, **kwargs: Any) -> web.StreamResponse:
        """Handle a POST."""
        return await self._guarded(request, **kwargs)

    async def put(self, request: web.Request, **kwargs: Any) -> web.StreamResponse:
        """Handle a PUT."""
        return await self._guarded(request, **kwargs)

    async def patch(self, request: web.Request, **kwargs: Any) -> web.StreamResponse:
        """Handle a PATCH."""
        return await self._guarded(request, **kwargs)

    async def delete(self, request: web.Request, **kwargs: Any) -> web.StreamResponse:
        """Handle a DELETE."""
        return await self._guarded(request, **kwargs)

    async def head(self, request: web.Request, **kwargs: Any) -> web.StreamResponse:
        """Handle a HEAD."""
        return await self._guarded(request, **kwargs)

    async def _guarded(self, request: web.Request, **kwargs: Any) -> web.StreamResponse:
        """Run the request, turning upstream failures into a 502."""
        try:
            return await self._handle_request(request, **kwargs)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Frigate proxy error for %s: %s", request.rel_url, err)
        return web.Response(status=HTTPStatus.BAD_GATEWAY)

    def _get_proxied_url(self, request: web.Request, **kwargs: Any) -> ProxiedURL:
        """Authorise the request and map it onto a Frigate URL."""
        entry_id: str = kwargs["entry_id"]
        data = self._registry.get(entry_id)
        if data is None or not self._is_authorised(request, data):
            raise HASSWebProxyLibUnauthorizedRequestError

        path = (kwargs.get("path") or "").lstrip("/")
        headers = {
            # Frigate's nginx rebases its own asset URLs, injects
            # window.baseUrl and rewrites the webmanifest based on this header.
            # Without it the SPA requests /assets/... at Home Assistant's root
            # instead of under our proxy prefix, and nothing loads.
            "X-Ingress-Path": f"{PROXY_BASE}/{entry_id}",
        }

        return ProxiedURL(
            url=f"{data.url.rstrip('/')}/{path}",
            query_params=request.query,
            # Already authorised above, by bearer token or by our own session
            # cookie. The library would otherwise reject cookie-only requests,
            # because Home Assistant's middleware cannot see them.
            allow_unauthenticated=True,
            headers=headers,
            ssl_context=(
                get_default_context()
                if data.verify_ssl
                else get_default_no_verify_context()
            ),
        )

    def _is_authorised(self, request: web.Request, data: PanelData) -> bool:
        """Accept a bearer-authenticated request, or a live session cookie."""
        if request.get(KEY_AUTHENTICATED):
            return True

        token = request.cookies.get(COOKIE_NAME)
        session = data.sessions.validate(token)
        if session is None or token is None:
            return False

        # Re-check the Home Assistant credential behind the cookie on every
        # request, so revoking a user's session in Home Assistant immediately
        # revokes their access to the panel rather than leaving the cookie
        # usable for the rest of its lifetime.
        if data.hass.auth.async_get_refresh_token(session.refresh_token_id) is None:
            data.sessions.revoke(token)
            return False

        return True

    async def _handle_request(
        self, request: web.Request, **kwargs: Any
    ) -> web.StreamResponse:
        """Dispatch to the websocket or the HTTP path."""
        if request.headers.get(hdrs.UPGRADE, "").lower() == "websocket":
            return await super()._handle_request(request, **kwargs)
        return await self._handle_http(request, **kwargs)

    async def _handle_http(
        self, request: web.Request, **kwargs: Any
    ) -> web.StreamResponse:
        """Serve an HTTP request, from cache when possible."""
        url_or_response = self._get_proxied_url_or_handle_error(request, **kwargs)
        if isinstance(url_or_response, web.Response):
            return url_or_response
        proxied = url_or_response

        data = self._registry.get(kwargs["entry_id"])
        if data is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)

        path = (kwargs.get("path") or "").lstrip("/")
        policy = classify(path, request.method, data.event_media_ttl)

        # A Range request yields a 206 covering part of the body. Storing that
        # as though it were the whole resource would corrupt later full reads
        # and break mp4 seeking, so ranged requests always bypass the cache.
        if policy.tier is Tier.STREAM or hdrs.RANGE in request.headers:
            return await self._relay(request, proxied)

        key = cache_key(data.namespace, path, request.query_string)
        stored = await data.hass.async_add_executor_job(data.cache.read, key)
        if stored is not None:
            if _client_holds_entity(request, stored.headers):
                return web.Response(
                    status=HTTPStatus.NOT_MODIFIED,
                    headers=_hit_headers(stored.headers, stored.age, policy.ttl),
                )
            return _cached_response(request, stored, policy)

        return await self._fetch_and_cache(request, proxied, policy, key, data)

    async def _relay(
        self, request: web.Request, proxied: ProxiedURL
    ) -> web.StreamResponse:
        """Relay upstream without buffering or caching.

        Live view, recordings and HLS all come through here, so nothing on this
        path may accumulate the body.
        """
        async with self._open_upstream(request, proxied) as result:
            response = web.StreamResponse(
                status=result.status, headers=_relay_headers(result)
            )
            response.content_type = result.content_type or "application/octet-stream"
            await response.prepare(request)
            try:
                async for chunk in result.content.iter_any():
                    await response.write(chunk)
            except (aiohttp.ClientError, ConnectionError) as err:
                _LOGGER.debug("Stream ended for %s: %s", request.rel_url, err)
            return response

    async def _fetch_and_cache(
        self,
        request: web.Request,
        proxied: ProxiedURL,
        policy: Policy,
        key: str,
        data: PanelData,
    ) -> web.StreamResponse:
        """Stream a response to the client, caching it on the way past."""
        async with self._open_upstream(request, proxied) as result:
            ttl = _ttl_for(result, policy)
            buffer: bytearray | None = bytearray() if ttl is not None else None

            # Recordings and exports are far too big to hold; stream those.
            declared = result.headers.get(hdrs.CONTENT_LENGTH, "")
            if (
                buffer is not None
                and declared.isdigit()
                and int(declared) > MAX_ENTRY_BYTES
            ):
                buffer = None

            headers = _relay_headers(result)
            headers[_CACHE_STATUS_HEADER] = "miss" if buffer is not None else "bypass"
            if buffer is not None and policy.ttl is not None:
                # Frigate serves event media with no cache headers at all, so
                # this is what stops browsers re-fetching every thumbnail.
                headers[hdrs.CACHE_CONTROL] = f"public, max-age={policy.ttl}"

            storable = _storable_headers(result) if buffer is not None else {}

            response = web.StreamResponse(status=result.status, headers=headers)
            response.content_type = result.content_type or "application/octet-stream"
            await response.prepare(request)

            try:
                async for chunk in result.content.iter_any():
                    await response.write(chunk)
                    if buffer is not None:
                        buffer.extend(chunk)
                        if len(buffer) > MAX_ENTRY_BYTES:
                            buffer = None
            except (aiohttp.ClientError, ConnectionError) as err:
                _LOGGER.debug("Stream ended for %s: %s", request.rel_url, err)
                buffer = None  # A truncated body must never be cached.

            status = result.status

        if buffer is not None and ttl is not None:
            await data.hass.async_add_executor_job(
                data.cache.write, key, status, storable, bytes(buffer), ttl
            )
        return response

    def _open_upstream(self, request: web.Request, proxied: ProxiedURL) -> Any:
        """Open the upstream request, streaming any request body through."""
        return self._websession.request(
            request.method,
            proxied.url,
            headers=_upstream_headers(request, proxied.headers),
            params=proxied.query_params,
            allow_redirects=False,
            data=request.content if request.can_read_body else None,
            ssl=proxied.ssl_context or get_default_context(),
        )


def _upstream_headers(request: web.Request, extra: Any = None) -> CIMultiDict[str]:
    """Build headers for the outgoing request to Frigate."""
    headers: CIMultiDict[str] = CIMultiDict(
        (name, value)
        for name, value in request.headers.items()
        if name not in _DROP_FROM_REQUEST
    )

    # Keep Frigate's own cookies working (it has optional built-in auth) while
    # not forwarding our session credential further than it needs to go.
    if hdrs.COOKIE in headers:
        del headers[hdrs.COOKIE]
    remaining = "; ".join(
        f"{name}={value}"
        for name, value in request.cookies.items()
        if name != COOKIE_NAME
    )
    if remaining:
        headers[hdrs.COOKIE] = remaining

    forwarded_for = request.headers.get(hdrs.X_FORWARDED_FOR)
    if request.transport is not None:
        peer = request.transport.get_extra_info("peername")
        if peer:
            client = ip_address(peer[0])
            headers[hdrs.X_FORWARDED_FOR] = (
                f"{forwarded_for}, {client}" if forwarded_for else str(client)
            )
    headers[hdrs.X_FORWARDED_HOST] = (
        request.headers.get(hdrs.X_FORWARDED_HOST) or request.host
    )
    headers[hdrs.X_FORWARDED_PROTO] = (
        request.headers.get(hdrs.X_FORWARDED_PROTO) or request.url.scheme
    )

    if extra:
        headers.update(CIMultiDict(extra))
    return headers


def _relay_headers(result: aiohttp.ClientResponse) -> dict[str, str]:
    """Build response headers for a streamed relay.

    aiohttp transparently decompresses the upstream body, so a Content-Length
    copied from a compressed response would not match what we actually write.
    It is kept only when upstream sent the body uncompressed, because mp4
    seeking depends on it.
    """
    headers = {
        name: value
        for name, value in result.headers.items()
        if name not in _DROP_FROM_RESPONSE
    }
    if hdrs.CONTENT_ENCODING in result.headers:
        headers.pop(hdrs.CONTENT_LENGTH, None)
    return headers


def _storable_headers(result: aiohttp.ClientResponse) -> dict[str, str]:
    """Build the header set to persist alongside a cached body."""
    headers = {
        name: value
        for name, value in result.headers.items()
        if name not in _DROP_FROM_RESPONSE
    }
    # The stored body length is authoritative when read back.
    headers.pop(hdrs.CONTENT_LENGTH, None)
    headers.pop(hdrs.SET_COOKIE, None)
    headers[hdrs.CONTENT_TYPE] = result.headers.get(
        hdrs.CONTENT_TYPE, "application/octet-stream"
    )
    return headers


def _ttl_for(result: aiohttp.ClientResponse, policy: Policy) -> int | None:
    """Decide how long to store a response, or None to not store it."""
    if result.status != HTTPStatus.OK:
        return None
    if hdrs.SET_COOKIE in result.headers:
        return None

    # A Vary means the body depends on request headers we do not key on.
    vary = result.headers.get(hdrs.VARY, "").strip()
    if vary and vary.lower() != "accept-encoding":
        return None

    cache_control = result.headers.get(hdrs.CACHE_CONTROL)

    # An explicit no-store from Frigate is always obeyed, even on paths we
    # would otherwise force-cache.
    if forbids_storage(cache_control):
        return None

    if policy.ttl is not None:
        return policy.ttl

    freshness = parse_cache_control(cache_control)
    if not freshness.storable:
        return None
    return freshness.max_age if freshness.max_age is not None else _DEFAULT_TTL


def _client_holds_entity(request: web.Request, stored: dict[str, str]) -> bool:
    """Report whether the client's If-None-Match matches the cached entity."""
    etag = stored.get(hdrs.ETAG)
    candidates = request.headers.get(hdrs.IF_NONE_MATCH, "")
    if not etag or not candidates:
        return False
    wanted = etag.removeprefix("W/")
    return any(
        tag.strip().removeprefix("W/") == wanted for tag in candidates.split(",")
    )


def _hit_headers(
    stored: dict[str, str], age: int, forced_ttl: int | None
) -> dict[str, str]:
    """Response headers for a cache hit."""
    headers = {
        name: value for name, value in stored.items() if name != hdrs.CONTENT_TYPE
    }
    headers[_CACHE_STATUS_HEADER] = "hit"
    headers["Age"] = str(age)
    if forced_ttl is not None:
        headers[hdrs.CACHE_CONTROL] = f"public, max-age={max(forced_ttl - age, 0)}"
    return headers


def _cached_response(
    request: web.Request, stored: Stored, policy: Policy
) -> web.Response:
    """Build a response from a cached entry."""
    response = web.Response(
        status=stored.status,
        body=stored.body,
        content_type=stored.headers.get(hdrs.CONTENT_TYPE, "application/octet-stream"),
        headers=_hit_headers(stored.headers, stored.age, policy.ttl),
    )

    # Bodies are stored decompressed, so re-apply compression per response
    # rather than keeping a cache entry per content coding. aiohttp compresses
    # in the writer and sets Content-Encoding and Vary itself.
    if (
        policy.compressible
        and len(stored.body) >= _GZIP_MIN_BYTES
        and "gzip" in request.headers.get(hdrs.ACCEPT_ENCODING, "").lower()
    ):
        response.enable_compression()

    return response
