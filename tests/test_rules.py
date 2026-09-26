"""Cache classification rules."""

import pytest

from custom_components.frigate_panel.rules import (
    Tier,
    classify,
    forbids_storage,
    parse_cache_control,
)

TTL = 600


@pytest.mark.parametrize(
    "path",
    [
        # Live view and streaming must never be buffered.
        "ws",
        "live/jsmpeg/front_door",
        "live/mse/api/ws",
        "live/webrtc/api/ws",
        "api/go2rtc/webrtc",
        "api/front_door/latest.jpg",
        "api/front_door/latest.webp",
        "api/front_door/mjpeg",
        "vod/2024-01/front_door/index.m3u8",
        "api/vod/event/1234.5-abc/index.m3u8",
        "stream/front_door/stream.m3u8",
        "api/stats",
        "api/preview/front_door/start/1/end/2",
        # Credentials and large exports.
        "api/login",
        "api/logout",
        "api/auth/first_time_login",
        "api/exports",
        "api/front_door/recordings/1700000000/1700000100/clip.mp4",
    ],
)
def test_never_cached(path):
    """Streams, live images and auth endpoints are passed straight through."""
    assert classify(path, "GET", TTL).tier is Tier.STREAM


def test_latest_jpg_is_not_confused_with_a_snapshot():
    """A live frame and an immutable event image differ only by name."""
    assert classify("api/front_door/latest.jpg", "GET", TTL).tier is Tier.STREAM
    assert classify("api/events/1.2-ab/snapshot.jpg", "GET", TTL).ttl == TTL


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD"])
def test_only_get_is_cacheable(method):
    """A write must never be served from or stored in the cache."""
    assert classify("api/config", method, TTL).tier is Tier.STREAM


@pytest.mark.parametrize(
    "path",
    [
        "api/events/1700000000.123456-abcdef/thumbnail.jpg",
        "api/events/1700000000.123456-abcdef/snapshot.jpg",
        "api/events/1700000000.123456-abcdef/snapshot.webp",
        "api/review/1700000000.123456-abcdef/thumbnail.jpg",
    ],
)
def test_event_images_get_the_configured_ttl(path):
    """These are what a review list re-fetches on every scroll."""
    policy = classify(path, "GET", TTL)
    assert policy.tier is Tier.CACHE
    assert policy.ttl == TTL
    assert policy.compressible is False


@pytest.mark.parametrize(
    "path",
    [
        "api/events/1700000000.123456-abcdef/clip.mp4",
        "api/review/1700000000.123456-abcdef/preview.gif",
    ],
)
def test_finished_media_is_immutable(path):
    """Clips only exist once the event has ended, so they never change."""
    policy = classify(path, "GET", TTL)
    assert policy.tier is Tier.CACHE
    assert policy.ttl == 365 * 24 * 60 * 60


@pytest.mark.parametrize(
    "path", ["", "index.html", "assets/index-a1b2c3.js", "locales/en/common.json"]
)
def test_upstream_decides_for_everything_else(path):
    """No forced TTL means Frigate's own headers are honoured."""
    policy = classify(path, "GET", TTL)
    assert policy.tier is Tier.CACHE
    assert policy.ttl is None


def test_precompressed_formats_are_not_recompressed():
    """Re-gzipping a JPEG or a font wastes CPU for nothing."""
    assert classify("assets/font-ab12.woff2", "GET", TTL).compressible is False
    assert classify("assets/index-a1b2c3.js", "GET", TTL).compressible is True


@pytest.mark.parametrize(
    ("header", "storable", "max_age"),
    [
        ("public, max-age=31536000", True, 31536000),
        ("public", True, None),
        ("max-age=60", True, 60),
        ("s-maxage=120, max-age=30", True, 120),
        ("no-store", False, None),
        ("private, max-age=60", False, None),
        ("no-cache", False, None),
        ("public, max-age=0", False, None),
        ("", False, None),
        (None, False, None),
    ],
)
def test_parse_cache_control(header, storable, max_age):
    """Upstream freshness is read straight off the header."""
    freshness = parse_cache_control(header)
    assert freshness.storable is storable
    if storable:
        assert freshness.max_age == max_age


@pytest.mark.parametrize(
    ("header", "forbidden"),
    [
        ("no-store", True),
        ("private", True),
        ("no-cache", True),
        ("public, max-age=60", False),
        (None, False),
    ],
)
def test_forbids_storage(header, forbidden):
    """An explicit refusal from upstream overrides any forced TTL."""
    assert forbids_storage(header) is forbidden
