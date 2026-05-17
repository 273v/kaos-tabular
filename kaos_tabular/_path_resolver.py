"""Thin tabular-flavoured wrapper around :func:`kaos_core.path_resolver.resolve_input_path`.

``RegisterTool`` and ``ReadFileTool`` both accept a ``path`` argument
from agent input. Historically they called raw ``Path(p).exists()``,
which is blind to anything an agentic UI host has uploaded into
``KaosRuntime.vfs`` (the SPA upload flow) and blind to opaque
``kaos://artifacts/<id>`` references the agent received from a sibling
tool. The production hallucination incident behind
``kaos-modules/docs/plans/vfs-blind-tools-audit-and-fix-plan.md`` was
ultimately a symptom of that blindness: every file-based tool returned
``{"error": true, "message": "File not found"}`` and the agent papered
over the cascade with fabricated NDA analysis.

The shared :func:`kaos_core.path_resolver.resolve_input_path` resolves
all four input shapes (artifact URI / kaos:// URI / VFS-relative path /
absolute path) into a real ``pathlib.Path`` that DuckDB and the
``_read_file`` helper can open unchanged. This module just pins the
tabular-specific MIME allow-list and the ``cli`` session fallback so
both tools can call one async context manager from their
``execute()`` body.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from kaos_core.path_resolver import resolve_input_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kaos_core.base.context import KaosContext
    from kaos_core.path_resolver import ResolvedInput

# Tabular tools accept CSV / TSV / Parquet / SQLite / JSON / JSONL / XLSX.
# XLSX is included so an agent that uploaded a spreadsheet still sees a
# clear "wrong reader, try kaos-office-parse-xlsx" error path from the
# resolver instead of a downstream DuckDB exception once the engine
# reaches the .xlsx and asks the user to install a separate reader.
_TABULAR_MIMES: tuple[str, ...] = (
    "text/csv",
    "text/tab-separated-values",
    "application/vnd.apache.parquet",
    "application/x-parquet",
    "application/vnd.sqlite3",
    "application/json",
    "application/x-ndjson",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)


@asynccontextmanager
async def resolve_tabular_input(
    path_or_uri: str,
    context: KaosContext | None,
) -> AsyncIterator[ResolvedInput]:
    """Resolve an agent-supplied tabular path/URI to an on-disk file.

    Yields a :class:`~kaos_core.path_resolver.ResolvedInput` whose
    ``path`` is a real :class:`pathlib.Path`. For artifact / VFS
    sources the bytes are streamed to a private temp file that is
    cleaned up on context exit; for absolute filesystem sources the
    path is returned untouched and no cleanup runs.

    DuckDB's ``CREATE TABLE ... AS SELECT * FROM read_csv(...)`` is
    eager: ``register_file`` materialises every row into a DuckDB
    table at register time, so the temp file can disappear the moment
    we exit the ``async with`` without breaking subsequent queries.
    Likewise ``_read_file`` builds a ``TabularDocument`` synchronously
    before returning, so the temp file's lifetime only needs to cover
    that single call. Both tools therefore safely call the engine /
    reader inside the ``async with`` block.

    When ``context`` is ``None`` (CLI / ephemeral-engine call sites)
    a stub :class:`KaosContext` with ``session_id="cli"`` is created
    so the resolver still has a session to scope filesystem fallbacks
    through. Artifact-store and session-scoped VFS reads in that mode
    will fail cleanly with an agent-friendly error since neither is
    attached to a stub context — that mirrors how those callers
    behaved before this refactor.
    """
    if context is None:
        from kaos_core.base.context import KaosContext as _KC

        context = _KC(session_id="cli")
    async with resolve_input_path(
        path_or_uri,
        context=context,
        allowed_mime_types=_TABULAR_MIMES,
    ) as resolved:
        yield resolved


__all__ = ["resolve_tabular_input"]
