"""Size-capped LRU disk cache for proxied Frigate responses.

Every entry is a single file so there is no window in which metadata and body
can disagree: the header and body are written to a temp file and moved into
place atomically. Recency is tracked with the file's mtime, which survives a
Home Assistant restart, so a restart does not throw the cache away.

All methods here block on file I/O. Callers are expected to run them in an
executor -- this module deliberately knows nothing about Home Assistant or
asyncio so it can be unit-tested on its own.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_MAGIC = b"FPC1"
_HEADER_LEN = struct.Struct(">I")


def cache_key(namespace: str, path: str, query: str) -> str:
    """Derive a cache key.

    ``namespace`` scopes entries to one config entry: Frigate rewrites asset
    URLs based on the X-Ingress-Path we send, so the same path served for a
    different entry is a different response body.

    Bodies are always stored decompressed, so the client's Accept-Encoding does
    not vary the key -- compression is re-applied per response on the way out.
    """
    return hashlib.sha256(
        b"\x00".join(p.encode() for p in (namespace, path, query))
    ).hexdigest()


@dataclass(frozen=True)
class Stored:
    """A response read back out of the cache."""

    status: int
    headers: dict[str, str]
    body: bytes
    stored_at: float
    age: int


class DiskCache:
    """LRU cache of proxied responses, capped by total bytes on disk."""

    def __init__(self, root: Path, budget_bytes: int) -> None:
        """Initialise the cache rooted at ``root``."""
        self._root = root
        self._budget = budget_bytes
        self._sizes: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        """Directory backing this cache."""
        return self._root

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / key[2:]

    def prime(self) -> None:
        """Scan the cache directory to rebuild the size index."""
        sizes: dict[str, int] = {}
        if self._root.is_dir():
            for shard in self._root.iterdir():
                if not shard.is_dir():
                    continue
                for entry in shard.iterdir():
                    if entry.suffix == ".tmp":
                        entry.unlink(missing_ok=True)
                        continue
                    try:
                        sizes[shard.name + entry.name] = entry.stat().st_size
                    except OSError:
                        continue
        with self._lock:
            self._sizes = sizes
        self._evict()

    def read(self, key: str) -> Stored | None:
        """Return a fresh entry, or None on miss or expiry."""
        path = self._path(key)
        try:
            with path.open("rb") as handle:
                if handle.read(4) != _MAGIC:
                    self._discard(key)
                    return None
                (length,) = _HEADER_LEN.unpack(handle.read(_HEADER_LEN.size))
                meta = json.loads(handle.read(length))
                body = handle.read()
        except OSError, ValueError, struct.error, json.JSONDecodeError:
            self._discard(key)
            return None

        stored_at = float(meta["stored_at"])
        age = time.time() - stored_at
        if age > float(meta["ttl"]):
            self._discard(key)
            return None

        # Touch for LRU. Best-effort: a read-only cache dir should not break
        # serving from it.
        with contextlib.suppress(OSError):
            os.utime(path, None)

        return Stored(
            status=int(meta["status"]),
            headers=dict(meta["headers"]),
            body=body,
            stored_at=stored_at,
            age=max(0, int(age)),
        )

    def write(
        self,
        key: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
        ttl: int,
    ) -> None:
        """Store a response, then evict down to the size budget."""
        meta = json.dumps(
            {
                "status": status,
                "headers": headers,
                "ttl": ttl,
                "stored_at": time.time(),
            }
        ).encode()

        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as handle:
                handle.write(_MAGIC)
                handle.write(_HEADER_LEN.pack(len(meta)))
                handle.write(meta)
                handle.write(body)
            tmp.replace(path)
        except OSError as err:
            _LOGGER.debug("Could not cache %s: %s", key, err)
            tmp.unlink(missing_ok=True)
            return

        with self._lock:
            self._sizes[key] = 4 + _HEADER_LEN.size + len(meta) + len(body)
        self._evict()

    def clear(self) -> None:
        """Remove every cached entry."""
        shutil.rmtree(self._root, ignore_errors=True)
        with self._lock:
            self._sizes.clear()

    def stats(self) -> dict[str, int]:
        """Report cache occupancy."""
        with self._lock:
            return {
                "entries": len(self._sizes),
                "bytes": sum(self._sizes.values()),
                "budget_bytes": self._budget,
            }

    def _discard(self, key: str) -> None:
        """Drop a corrupt or stale entry."""
        self._path(key).unlink(missing_ok=True)
        with self._lock:
            self._sizes.pop(key, None)

    def _evict(self) -> None:
        """Evict least-recently-used entries until inside the budget."""
        with self._lock:
            total = sum(self._sizes.values())
            if total <= self._budget:
                return
            keys = list(self._sizes)

        # mtime is the access time (read() touches it), so oldest first is LRU.
        aged: list[tuple[float, str]] = []
        for key in keys:
            try:
                aged.append((self._path(key).stat().st_mtime, key))
            except OSError:
                self._discard(key)
        aged.sort()

        for _, key in aged:
            with self._lock:
                if total <= self._budget:
                    return
                total -= self._sizes.get(key, 0)
            self._discard(key)
