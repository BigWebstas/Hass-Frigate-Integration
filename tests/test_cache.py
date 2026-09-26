"""Disk cache behaviour."""

import os
import time

import pytest

from custom_components.frigate_panel.cache import DiskCache, cache_key

HEADERS = {"Content-Type": "image/jpeg"}


@pytest.fixture
def cache(tmp_path):
    """A cache with a generous budget."""
    return DiskCache(root=tmp_path / "cache", budget_bytes=1024 * 1024)


def test_round_trip(cache):
    """A stored response comes back byte for byte."""
    cache.write("k" * 64, 200, HEADERS, b"\xff\xd8body", ttl=60)
    stored = cache.read("k" * 64)
    assert stored is not None
    assert stored.status == 200
    assert stored.body == b"\xff\xd8body"
    assert stored.headers == HEADERS


def test_miss_returns_none(cache):
    """An absent key is a miss, not an error."""
    assert cache.read("0" * 64) is None


def test_expiry(cache):
    """An entry past its TTL is a miss and is removed."""
    key = "a" * 64
    cache.write(key, 200, HEADERS, b"body", ttl=0)
    time.sleep(0.01)
    assert cache.read(key) is None
    assert cache.stats()["entries"] == 0


def test_age_is_reported(cache):
    """Age lets the response carry a correct remaining max-age."""
    key = "b" * 64
    cache.write(key, 200, HEADERS, b"body", ttl=60)
    stored = cache.read(key)
    assert stored is not None
    assert stored.age >= 0


def test_corrupt_entry_is_discarded(cache):
    """A truncated file must not raise on read."""
    key = "c" * 64
    cache.write(key, 200, HEADERS, b"body", ttl=60)
    path = cache.root / key[:2] / key[2:]
    path.write_bytes(b"garbage")
    assert cache.read(key) is None
    assert not path.exists()


def test_eviction_respects_budget(tmp_path):
    """Oldest entries go first once the cache is over budget."""
    cache = DiskCache(root=tmp_path / "cache", budget_bytes=4096)
    body = b"x" * 1000

    for index in range(3):
        key = f"{index:064d}"
        cache.write(key, 200, HEADERS, body, ttl=600)
        # Distinct mtimes so "least recently used" is unambiguous.
        os.utime(cache.root / key[:2] / key[2:], (1000 + index, 1000 + index))

    assert cache.stats()["entries"] == 3

    # This pushes total size past the 4096-byte budget.
    cache.write(f"{9:064d}", 200, HEADERS, body, ttl=600)

    assert cache.stats()["bytes"] <= 4096
    # The oldest entry is the one that went.
    assert cache.read(f"{0:064d}") is None
    assert cache.read(f"{9:064d}") is not None


def test_reading_an_entry_protects_it_from_eviction(tmp_path):
    """A hit refreshes recency, so hot entries survive."""
    cache = DiskCache(root=tmp_path / "cache", budget_bytes=3500)
    body = b"y" * 1000

    for index in range(3):
        key = f"{index:064d}"
        cache.write(key, 200, HEADERS, body, ttl=600)
        os.utime(cache.root / key[:2] / key[2:], (1000 + index, 1000 + index))

    # Touch the oldest entry, making the second-oldest the eviction candidate.
    assert cache.read(f"{0:064d}") is not None

    cache.write(f"{9:064d}", 200, HEADERS, body, ttl=600)

    assert cache.read(f"{0:064d}") is not None
    assert cache.read(f"{1:064d}") is None


def test_prime_rebuilds_the_index_across_restarts(tmp_path):
    """A restart must not throw away a warm cache."""
    root = tmp_path / "cache"
    first = DiskCache(root=root, budget_bytes=1024 * 1024)
    first.write("d" * 64, 200, HEADERS, b"body", ttl=600)

    second = DiskCache(root=root, budget_bytes=1024 * 1024)
    second.prime()
    assert second.stats()["entries"] == 1
    assert second.read("d" * 64) is not None


def test_prime_clears_partial_writes(tmp_path):
    """A temp file left by a crash mid-write is cleaned up."""
    root = tmp_path / "cache"
    cache = DiskCache(root=root, budget_bytes=1024)
    cache.write("e" * 64, 200, HEADERS, b"body", ttl=600)
    leftover = root / "ee" / (("e" * 62) + ".tmp")
    leftover.write_bytes(b"partial")

    cache.prime()
    assert not leftover.exists()


def test_clear_removes_everything(cache):
    """Removing the config entry must leave nothing behind."""
    cache.write("f" * 64, 200, HEADERS, b"body", ttl=600)
    cache.clear()
    assert cache.stats()["entries"] == 0
    assert cache.read("f" * 64) is None


def test_zero_budget_stores_nothing(tmp_path):
    """Setting the cache size to 0 disables it."""
    cache = DiskCache(root=tmp_path / "cache", budget_bytes=0)
    cache.write("g" * 64, 200, HEADERS, b"body", ttl=600)
    assert cache.read("g" * 64) is None


def test_key_varies_by_namespace_and_query():
    """Frigate rewrites bodies per ingress path, so the entry must be keyed in."""
    assert cache_key("entry-a", "api/config", "") != cache_key(
        "entry-b", "api/config", ""
    )
    assert cache_key("e", "api/events", "camera=front") != cache_key(
        "e", "api/events", "camera=back"
    )
    assert cache_key("e", "api/config", "") == cache_key("e", "api/config", "")
