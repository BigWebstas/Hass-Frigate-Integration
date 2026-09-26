"""Cache policy for proxied Frigate paths.

Frigate's own nginx already declares what is cacheable (`/assets/` gets
`expires 1y`, everything under `/api/` gets `no-store`), so the default stance
here is to honour the upstream headers rather than maintain a parallel
taxonomy. Two deliberate departures from that:

* Paths that stream (live view, recordings, HLS) are never cached, decided
  before we talk to Frigate so we never buffer a stream.
* Event media (thumbnails, snapshots, clips) is served by a separate nginx
  location that sets no cache headers at all, so browsers re-fetch every
  thumbnail on every list scroll. Those get an explicit TTL, which is the
  single biggest win this integration provides.

Nothing here imports Home Assistant, so it is directly unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Tier(StrEnum):
    """How a given path should be handled."""

    STREAM = "stream"
    """Relay unbuffered, never cache."""

    CACHE = "cache"
    """Eligible for the disk cache."""


@dataclass(frozen=True)
class Policy:
    """What to do with a response for a given path."""

    tier: Tier

    ttl: int | None = None
    """Forced cache lifetime in seconds.

    ``None`` means "cache only if Frigate's own headers say it is cacheable,
    for as long as they say". A number means "cache for this long even if
    Frigate sent no cache headers" -- still subject to an explicit `no-store`
    from upstream, which is always obeyed.
    """

    compressible: bool = True
    """False for formats that are already compressed (JPEG, MP4)."""


_STREAM = Policy(Tier.STREAM)

# Anything that streams, is per-request live state, or is an auth endpoint.
# Checked first, so a live `latest.jpg` never lands in the cache next to an
# immutable event snapshot.
_NEVER_CACHE = re.compile(
    r"""
      ^ws$ | ^ws/
    | ^live/
    | ^stream/
    | ^vod/
    | ^api/vod/
    | ^api/go2rtc/
    | ^api/preview/
    | ^api/notifications
    | ^api/stats
    | ^api/(login|logout|profile|auth)\b
    | ^api/restart\b
    | ^exports?/ | ^api/exports?\b
    | ^recordings/ | ^api/[^/]+/recordings\b
    | ^clips/
    | latest\.(jpg|jpeg|png|webp|mp4)$
    | mjpeg
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Written once when the event is created and never rewritten.
_IMMUTABLE_MEDIA = re.compile(
    r"^api/events/[^/]+/clip\.(mp4|m4v)$"
    r"|^api/review/[^/]+/preview\.(gif|mp4|jpg|jpeg|webp)$",
    re.IGNORECASE,
)

# Thumbnails and snapshots keep improving while an event is still in progress,
# so these get a moderate TTL rather than being treated as immutable.
_EVENT_IMAGE = re.compile(
    r"^api/events/[^/]+/(thumbnail|snapshot)\.(jpg|jpeg|png|webp)$"
    r"|^api/review/[^/]+/thumbnail\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)

_YEAR = 365 * 24 * 60 * 60

_PRECOMPRESSED = re.compile(
    r"\.(jpg|jpeg|png|webp|gif|mp4|m4v|m4s|ts|woff|woff2|zip|gz|br)$",
    re.IGNORECASE,
)


def classify(path: str, method: str, event_media_ttl: int) -> Policy:
    """Return the cache policy for a proxied request.

    ``path`` is relative to the proxy root with no leading slash, e.g.
    ``api/events/1234.5-abcd/thumbnail.jpg``.
    """
    if method.upper() != "GET":
        return _STREAM

    path = path.lstrip("/")

    if _NEVER_CACHE.search(path):
        return _STREAM

    compressible = not _PRECOMPRESSED.search(path)

    if _IMMUTABLE_MEDIA.match(path):
        return Policy(Tier.CACHE, ttl=_YEAR, compressible=compressible)

    if _EVENT_IMAGE.match(path):
        return Policy(Tier.CACHE, ttl=event_media_ttl, compressible=compressible)

    # Everything else -- index.html, /assets/, /locales/, API JSON -- defers to
    # whatever Frigate said about it.
    return Policy(Tier.CACHE, ttl=None, compressible=compressible)


@dataclass(frozen=True)
class Freshness:
    """The cacheability Frigate declared for a response."""

    storable: bool
    max_age: int | None = None


def parse_cache_control(value: str | None) -> Freshness:
    """Interpret an upstream Cache-Control header.

    A missing header is *not* storable on its own -- only an explicit policy
    TTL can make those cacheable.
    """
    if not value:
        return Freshness(storable=False)

    directives: dict[str, str | None] = {}
    for raw_part in value.split(","):
        part = raw_part.strip().lower()
        if not part:
            continue
        name, _, val = part.partition("=")
        directives[name.strip()] = val.strip().strip('"') or None

    if {"no-store", "private", "no-cache"} & directives.keys():
        return Freshness(storable=False)

    max_age: int | None = None
    raw = directives.get("s-maxage") or directives.get("max-age")
    if raw is not None:
        try:
            max_age = int(raw)
        except ValueError:
            max_age = None
        else:
            if max_age <= 0:
                return Freshness(storable=False)

    if max_age is None and "public" not in directives:
        return Freshness(storable=False)

    return Freshness(storable=True, max_age=max_age)


def forbids_storage(value: str | None) -> bool:
    """Report whether upstream explicitly told us not to store the response."""
    if not value:
        return False
    lowered = value.lower()
    return any(d in lowered for d in ("no-store", "private", "no-cache"))
