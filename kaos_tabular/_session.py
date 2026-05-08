"""Session-scoped TabularEngine cache.

Each MCP session gets its own :class:`~kaos_tabular.engine.TabularEngine`
keyed by ``KaosContext.session_id``. The :class:`EngineRegistry`
class owns that mapping behind a bounded LRU policy: when a new
session arrives and the cache is full, the oldest engine's DuckDB
connection is closed and the entry is evicted. This is a pragmatic
stand-in for proper session-end notification — kaos-mcp does not
yet expose a per-session lifecycle hook the engine could subscribe
to. Replace with a hook-driven close at 0.1.0a2.

The registry is exposed as a class (rather than a module-level
``dict``) so tests can construct fresh, isolated instances without
monkey-patching module globals, and so per-runtime overrides
(custom ``engine_factory`` for stub engines, alternative
``max_sessions`` for embedded use) become parameter passes rather
than global mutations.

The module-level :data:`SESSION_REGISTRY` is the singleton that
``kaos_tabular.tools`` uses for live MCP sessions; it preserves the
prior behaviour of one cache per running process.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

from kaos_tabular.engine import TabularEngine

if TYPE_CHECKING:
    pass

DEFAULT_MAX_SESSIONS = 64
"""Process-wide default cap on the LRU. Tunable per-instance via the
``EngineRegistry(max_sessions=...)`` constructor arg."""


class EngineRegistry:
    """Bounded LRU cache mapping ``session_id -> TabularEngine``.

    The cache enforces three invariants:

    1. The same ``session_id`` always maps to the same engine for as
       long as it stays warm.
    2. The number of cached engines never exceeds ``max_sessions``.
    3. When a session is evicted (oldest first), the engine's DuckDB
       connection is closed before the entry is removed.

    The registry is async-safe via an internal :class:`asyncio.Lock`;
    the cache is not thread-safe and is intended to live inside a
    single asyncio event loop.
    """

    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        engine_factory: Callable[[], TabularEngine] = TabularEngine,
    ) -> None:
        if max_sessions < 1:
            msg = f"max_sessions must be >= 1, got {max_sessions!r}"
            raise ValueError(msg)
        self._max_sessions = max_sessions
        self._engine_factory = engine_factory
        self._engines: OrderedDict[str, TabularEngine] = OrderedDict()
        self._lock = asyncio.Lock()

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    @property
    def session_ids(self) -> list[str]:
        """Snapshot of cached session IDs in LRU order (oldest first)."""
        return list(self._engines)

    def __len__(self) -> int:
        return len(self._engines)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._engines

    async def get(self, session_id: str) -> TabularEngine:
        """Return the engine for ``session_id``, creating it if missing.

        On a cache hit the entry is promoted to the most-recently-used
        end. On a miss past capacity the oldest engine is closed and
        its slot is reused.
        """
        async with self._lock:
            if session_id in self._engines:
                self._engines.move_to_end(session_id)
                return self._engines[session_id]
            while len(self._engines) >= self._max_sessions:
                _, evicted = self._engines.popitem(last=False)
                evicted.close()
            engine = self._engine_factory()
            self._engines[session_id] = engine
            return engine

    async def close_all(self) -> None:
        """Close every cached engine and clear the cache.

        Intended for shutdown paths and test teardown. Acquires the
        same lock ``get`` uses, so a concurrent ``get`` on the same
        registry sees an empty cache after this returns.
        """
        async with self._lock:
            while self._engines:
                _, engine = self._engines.popitem(last=False)
                engine.close()


SESSION_REGISTRY = EngineRegistry()
"""Singleton used by ``kaos_tabular.tools`` for MCP-session engines.

Tests should construct their own :class:`EngineRegistry` rather than
mutating this instance.
"""
