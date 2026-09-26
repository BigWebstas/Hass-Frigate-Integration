"""Panel session store."""

from custom_components.frigate_panel.session import SessionStore


def store(ttl=3600, max_sessions=8):
    """Build a store."""
    return SessionStore(ttl=ttl, max_sessions=max_sessions)


def test_minted_session_validates():
    """A freshly minted token identifies its user and credential."""
    sessions = store()
    token, ttl = sessions.mint("user-1", "refresh-1")
    assert ttl == 3600

    session = sessions.validate(token)
    assert session is not None
    assert session.user_id == "user-1"
    assert session.refresh_token_id == "refresh-1"


def test_unknown_and_empty_tokens_are_rejected():
    """A guessed or absent cookie gets nothing."""
    sessions = store()
    assert sessions.validate("nope") is None
    assert sessions.validate("") is None
    assert sessions.validate(None) is None


def test_tokens_are_unique_and_long():
    """Tokens must not be guessable."""
    sessions = store(max_sessions=64)
    tokens = {sessions.mint("u", "r")[0] for _ in range(32)}
    assert len(tokens) == 32
    assert all(len(token) >= 32 for token in tokens)


def test_expired_session_is_rejected_and_dropped():
    """A stale cookie stops working."""
    sessions = store(ttl=0)
    token, _ = sessions.mint("user-1", "refresh-1")
    assert sessions.validate(token) is None
    assert len(sessions) == 0


def test_revoke():
    """Revoking a session invalidates just that one."""
    sessions = store()
    first, _ = sessions.mint("user-1", "refresh-1")
    second, _ = sessions.mint("user-2", "refresh-2")

    sessions.revoke(first)
    assert sessions.validate(first) is None
    assert sessions.validate(second) is not None


def test_clear():
    """Unloading the entry invalidates everything."""
    sessions = store()
    token, _ = sessions.mint("user-1", "refresh-1")
    sessions.clear()
    assert sessions.validate(token) is None


def test_session_count_is_bounded():
    """A client looping the mint endpoint must not grow memory without limit."""
    sessions = store(max_sessions=4)
    tokens = [sessions.mint(f"user-{i}", f"refresh-{i}")[0] for i in range(20)]

    assert len(sessions) <= 4
    # The most recent session always survives.
    assert sessions.validate(tokens[-1]) is not None
