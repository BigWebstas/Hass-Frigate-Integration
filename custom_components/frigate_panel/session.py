"""Short-lived sessions that authenticate iframe sub-resource requests.

Home Assistant authenticates API requests with an ``Authorization: Bearer``
header or a signed ``?authSig=`` query parameter, and supports no cookie auth
at all. Neither mechanism can work here: the Frigate UI is a single-page app
inside an iframe, and the browser issues its own requests for assets, images
and websockets without any way for us to attach a header, and without us
knowing the URLs up front so we could sign them.

So this integration does what Supervisor ingress does -- mint a session bound
to the Home Assistant credential that asked for it, hand it back as a cookie
scoped to the proxy path, and accept that cookie on subsequent requests. The
token carries the refresh token id it came from so the proxy can re-check, on
every request, that the underlying Home Assistant credential is still valid.
Revoking a user's session in Home Assistant therefore also kills their access
to the panel, rather than leaving a cookie usable for the rest of its TTL.

Pure Python, no Home Assistant imports, so it is unit-testable on its own.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    """A minted panel session."""

    user_id: str
    refresh_token_id: str
    expires_at: float


class SessionStore:
    """In-memory store of panel sessions.

    Sessions are deliberately not persisted: a Home Assistant restart drops
    them, and the panel simply mints a new one on next load.
    """

    def __init__(self, ttl: int, max_sessions: int) -> None:
        """Initialise an empty store."""
        self._ttl = ttl
        self._max = max_sessions
        self._sessions: dict[str, Session] = {}

    def mint(self, user_id: str, refresh_token_id: str) -> tuple[str, int]:
        """Create a session, returning the token and its lifetime."""
        self._prune()
        if len(self._sessions) >= self._max:
            # Drop whatever expires soonest to bound memory.
            oldest = min(self._sessions, key=lambda t: self._sessions[t].expires_at)
            del self._sessions[oldest]

        token = secrets.token_urlsafe(32)
        self._sessions[token] = Session(
            user_id=user_id,
            refresh_token_id=refresh_token_id,
            expires_at=time.time() + self._ttl,
        )
        return token, self._ttl

    def validate(self, token: str | None) -> Session | None:
        """Return the session for a token, or None if absent or expired."""
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            del self._sessions[token]
            return None
        return session

    def revoke(self, token: str) -> None:
        """Invalidate a single session."""
        self._sessions.pop(token, None)

    def clear(self) -> None:
        """Invalidate every session."""
        self._sessions.clear()

    def __len__(self) -> int:
        """Count the live sessions."""
        self._prune()
        return len(self._sessions)

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[token]
