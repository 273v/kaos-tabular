"""Bounded-LRU session-engine cache.

The cache lives in :class:`kaos_tabular._session.EngineRegistry`. Each
test constructs its own registry (no globals to monkeypatch) and pins
the four LRU invariants:

1. Same session_id → same engine across calls (cache hit identity).
2. Distinct session_ids → distinct engines.
3. Cache hits refresh LRU position (touched session is no longer the
   eviction target).
4. Inserts past ``max_sessions`` close the oldest engine — both via a
   ``close_count`` spy on a real subclass and by checking the evicted
   engine's underlying DuckDB connection raises post-eviction.

A separate test asserts the bridge function in ``tools._get_engine``
delegates to the process singleton ``SESSION_REGISTRY`` for contexts
and falls back to an ephemeral engine when context is ``None``.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import duckdb
import pytest

from kaos_tabular import _session
from kaos_tabular import tools as t
from kaos_tabular._session import EngineRegistry
from kaos_tabular.engine import TabularEngine


def _ctx(session_id: str) -> Any:
    """Stand-in KaosContext exposing only the session_id we need."""
    ctx = mock.MagicMock()
    ctx.session_id = session_id
    return ctx


class _CountingEngine(TabularEngine):
    """Real ``TabularEngine`` that tracks ``close`` calls.

    Used by the eviction test instead of monkeypatching
    ``engine.close = lambda: ...`` on a base ``TabularEngine``: the
    subclass owns the contract, so any future change to ``close``
    flows through cleanly without re-rolling the spy.
    """

    def __init__(self) -> None:
        super().__init__()
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


# ---------------------------------------------------------------------------
# EngineRegistry — direct tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_caches_per_session() -> None:
    reg = EngineRegistry(max_sessions=8)
    try:
        e1 = await reg.get("a")
        e2 = await reg.get("a")
        assert e1 is e2
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_registry_distinct_sessions_distinct_engines() -> None:
    reg = EngineRegistry(max_sessions=8)
    try:
        e1 = await reg.get("a")
        e2 = await reg.get("b")
        assert e1 is not e2
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_registry_cache_hit_promotes_to_recent() -> None:
    """Touching a session refreshes its LRU position; the next eviction
    targets a different entry."""
    reg = EngineRegistry(max_sessions=3)
    try:
        for sid in ("a", "b", "c"):
            await reg.get(sid)
        # Touch 'a' — moves it to most-recent end.
        await reg.get("a")
        # Insert 'd' — should evict 'b' (now the oldest), not 'a'.
        await reg.get("d")
        assert reg.session_ids == ["c", "a", "d"]
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_registry_evicts_oldest_engine_and_closes_its_connection() -> None:
    """Inserting past ``max_sessions`` evicts the oldest, calls its
    ``close()``, and renders its DuckDB connection unusable.

    Uses the ``engine_factory`` constructor arg to plant a real
    ``_CountingEngine`` at the oldest slot — no monkeypatching, no
    reaching into the registry's private state.
    """
    counters: list[_CountingEngine] = []

    def factory() -> TabularEngine:
        engine = _CountingEngine()
        counters.append(engine)
        return engine

    reg = EngineRegistry(max_sessions=4, engine_factory=factory)
    try:
        for i in range(4):
            await reg.get(f"s{i}")
        assert reg.session_ids == ["s0", "s1", "s2", "s3"]
        assert all(c.close_count == 0 for c in counters)

        # Trigger eviction by registering one more session.
        await reg.get("s4")

        # s0 evicted; s4 most recent.
        assert reg.session_ids == ["s1", "s2", "s3", "s4"]

        # Exactly one engine (the s0 one) had close() called.
        close_counts = [c.close_count for c in counters]
        assert close_counts == [1, 0, 0, 0, 0], close_counts

        # The evicted engine's DuckDB connection is actually closed.
        with pytest.raises(duckdb.ConnectionException):
            counters[0]._con.execute("SELECT 1")
    finally:
        await reg.close_all()


@pytest.mark.asyncio
async def test_registry_close_all_closes_every_engine() -> None:
    """``close_all`` empties the cache and closes each engine."""
    counters: list[_CountingEngine] = []

    def factory() -> TabularEngine:
        engine = _CountingEngine()
        counters.append(engine)
        return engine

    reg = EngineRegistry(max_sessions=8, engine_factory=factory)
    for sid in ("a", "b", "c"):
        await reg.get(sid)
    assert len(reg) == 3

    await reg.close_all()
    assert len(reg) == 0
    assert all(c.close_count == 1 for c in counters)


def test_registry_rejects_invalid_max_sessions() -> None:
    with pytest.raises(ValueError, match="max_sessions must be >= 1"):
        EngineRegistry(max_sessions=0)
    with pytest.raises(ValueError, match="max_sessions must be >= 1"):
        EngineRegistry(max_sessions=-1)


# ---------------------------------------------------------------------------
# tools._get_engine bridge — delegates to the process singleton
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_session_registry() -> Any:
    """Replace the process-wide SESSION_REGISTRY for each bridge test.

    The bridge tests below exercise ``tools._get_engine`` which routes
    through the singleton. Using a fresh registry per test prevents
    cross-test bleed.
    """
    saved = _session.SESSION_REGISTRY
    fresh = EngineRegistry(max_sessions=DEFAULT_MAX_FOR_BRIDGE_TESTS)
    _session.SESSION_REGISTRY = fresh
    # tools.py imports SESSION_REGISTRY at import time (``from
    # kaos_tabular._session import SESSION_REGISTRY``), which binds
    # the name in the module namespace. Re-bind there too.
    t.SESSION_REGISTRY = fresh
    try:
        yield
    finally:
        # Best-effort cleanup; close_all is async but the fixture is
        # synchronous, so we just clear the name.
        _session.SESSION_REGISTRY = saved
        t.SESSION_REGISTRY = saved


DEFAULT_MAX_FOR_BRIDGE_TESTS = 8


@pytest.mark.asyncio
async def test_bridge_routes_to_singleton_for_context() -> None:
    e1 = await t._get_engine(_ctx("session-x"))
    e2 = await t._get_engine(_ctx("session-x"))
    assert e1 is e2
    assert "session-x" in _session.SESSION_REGISTRY


@pytest.mark.asyncio
async def test_bridge_returns_ephemeral_engine_for_no_context() -> None:
    e = await t._get_engine(None)
    assert "" not in _session.SESSION_REGISTRY  # nothing registered
    assert len(_session.SESSION_REGISTRY) == 0
    e.close()
