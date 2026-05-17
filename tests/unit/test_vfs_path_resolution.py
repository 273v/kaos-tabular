"""VFS-aware path resolution for ``kaos-tabular`` file-input tools.

These tests exercise the regression class behind the production
NDA-hallucination incident in session
``01KRVYAEA3B1HG95DBAG6H0DJ3``: an agent invoked
``kaos-tabular-register`` / ``kaos-tabular-read-file`` against a CSV
the user uploaded through ``kaos-ui``'s single-user-chat SPA. Both
tools resolved the path via raw ``Path(p).exists()`` against the
backend process CWD; the file lived inside the session VFS at
``.kaos-vfs/sessions/<sid>/files/<name>``; every call returned
``{"error": true, "message": "File not found"}``; the agent then
fabricated an analysis citing the files.

The fix routes both tools through
``kaos_core.path_resolver.resolve_input_path`` via the thin
``kaos_tabular._path_resolver.resolve_tabular_input`` wrapper. The
tests below pin all three documented input shapes for both tools:

* a VFS-relative path that mirrors the SPA upload layout exactly;
* a ``kaos://artifacts/<id>`` URI returned by a previous tool;
* a bogus path that lands in neither the VFS nor the local filesystem,
  which must surface as a three-part agent-friendly error rather than
  a Python stack trace.

Plan: ``kaos-modules/docs/plans/vfs-blind-tools-audit-and-fix-plan.md``
(Stage 3).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kaos_core import (
    ArtifactStore,
    KaosContext,
    KaosRuntime,
    KaosSettings,
    VFSConfig,
    VirtualFileSystem,
)
from kaos_core.types.enums import StorageBackend

from kaos_tabular.tools import ReadFileTool, RegisterTool

# ---------------------------------------------------------------------------
# Fixtures — production-shaped VFS-backed runtime
# ---------------------------------------------------------------------------

_SIMPLE_CSV = b"id,name,salary\n1,Alice,95000\n2,Bob,72000\n3,Charlie,110000\n"


def _make_runtime(tmp_path: Path) -> KaosRuntime:
    """Build a disk-VFS-backed runtime with an attached ArtifactStore.

    Mirrors the layout the SPA backend uses in production so the
    resolver sees the same session-isolation boundary.
    """
    settings = KaosSettings(
        artifact_inline_read_max_bytes=262_144,
        artifact_chunk_size_bytes=64,
    )
    runtime = KaosRuntime(config=settings)
    runtime.vfs = VirtualFileSystem(
        VFSConfig(default_backend=StorageBackend.DISK, disk_base_path=tmp_path / "vfs")
    )
    runtime.artifacts = ArtifactStore(
        runtime.vfs,
        manifest_context_id=settings.artifact_manifest_context_id,
        manifest_prefix=settings.artifact_manifest_prefix,
        max_inline_read_bytes=settings.artifact_inline_read_max_bytes,
        default_chunk_size=settings.artifact_chunk_size_bytes,
        temporary_ttl_seconds=settings.artifact_temporary_ttl_seconds,
    )
    return runtime


def _context(runtime: KaosRuntime, session_id: str = "s-test") -> KaosContext:
    return KaosContext(session_id=session_id, runtime=runtime, vfs=runtime.vfs)


# ---------------------------------------------------------------------------
# RegisterTool — VFS-relative path (SPA upload shape)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegisterVFSPath:
    """`kaos-tabular-register` must see files uploaded into the session VFS."""

    async def test_register_csv_from_vfs_relative_path(self, tmp_path: Path) -> None:
        runtime = _make_runtime(tmp_path)
        ctx = _context(runtime, session_id="s-spa-upload")
        # Mirrors SPA upload layout: backend writes uploads under
        # ``files/<name>`` inside the session VFS root.
        vfs_path = "files/employees.csv"
        await ctx.get_vfs_path(vfs_path).write_bytes(_SIMPLE_CSV)

        result = await RegisterTool().execute({"path": vfs_path}, context=ctx)

        assert not result.isError, f"Register failed: {result.text}"
        data = result.require_structured()
        assert data["table_name"] == "employees"
        assert data["row_count"] == 3
        assert data["column_count"] == 3
        col_names = {c["name"] for c in data["columns"]}
        assert col_names == {"id", "name", "salary"}

    async def test_register_query_roundtrip_via_vfs_path(self, tmp_path: Path) -> None:
        """Register via VFS path then query — confirms data is actually loaded.

        DuckDB's ``CREATE TABLE ... AS SELECT * FROM read_csv(...)`` is
        eager, so the temp file the resolver materialised can disappear
        without breaking subsequent queries. This regression test pins
        that property: the temp-file lifetime ends when the resolver's
        ``async with`` exits, but the query must still return rows.
        """
        from kaos_tabular.tools import QueryTool

        runtime = _make_runtime(tmp_path)
        ctx = _context(runtime, session_id="s-roundtrip")
        await ctx.get_vfs_path("files/employees.csv").write_bytes(_SIMPLE_CSV)

        reg = await RegisterTool().execute({"path": "files/employees.csv"}, context=ctx)
        assert not reg.isError

        q = await QueryTool().execute(
            {"sql": "SELECT name FROM employees WHERE salary > 80000 ORDER BY name"},
            context=ctx,
        )
        assert not q.isError, f"Query failed: {q.text}"
        text = q.require_text()
        assert "Alice" in text
        assert "Charlie" in text
        assert "Bob" not in text  # salary 72000 below threshold


# ---------------------------------------------------------------------------
# RegisterTool — kaos://artifacts/<id> URI
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegisterArtifactURI:
    """Accepts artifact URIs returned by a previous tool."""

    async def test_register_via_artifact_uri_echoes_artifact_id(self, tmp_path: Path) -> None:
        runtime = _make_runtime(tmp_path)
        ctx = _context(runtime, session_id="s-artifact-reg")
        manifest = await runtime.artifacts.create_from_bytes(
            _SIMPLE_CSV,
            context_id=ctx.session_id,
            session_id=ctx.session_id,
            name="employees.csv",
            mime_type="text/csv",
        )

        uri = f"kaos://artifacts/{manifest.artifact_id}"
        result = await RegisterTool().execute({"path": uri, "table_name": "employees"}, context=ctx)

        assert not result.isError, f"Register failed: {result.text}"
        data = result.require_structured()
        assert data["table_name"] == "employees"
        assert data["row_count"] == 3
        # Origin was an artifact → the id round-trips into structured output
        # so the SPA's ArtifactCard / downstream tools can re-resolve it.
        assert data.get("artifact_id") == manifest.artifact_id


# ---------------------------------------------------------------------------
# RegisterTool — clean three-part error for unresolvable paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegisterUnresolvable:
    """`InputPathResolutionError` surfaces as a chip-friendly error."""

    async def test_register_missing_path_returns_three_part_error(self, tmp_path: Path) -> None:
        runtime = _make_runtime(tmp_path)
        ctx = _context(runtime, session_id="s-missing")

        result = await RegisterTool().execute({"path": "files/does-not-exist.csv"}, context=ctx)

        assert result.isError
        text = result.text or ""
        # Three-part shape from InputPathResolutionError.to_agent_message():
        # what / How to fix / Alternative.
        assert "How to fix" in text
        assert "Alternative" in text
        # And it nudges the agent at the right next tool, not at the
        # same one again.
        assert "kaos-core-vfs-list" in text


# ---------------------------------------------------------------------------
# ReadFileTool — VFS-relative path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadFileVFSPath:
    """`kaos-tabular-read-file` must see VFS uploads and emit a TabularDocument."""

    async def test_read_file_from_vfs_relative_path(self, tmp_path: Path) -> None:
        runtime = _make_runtime(tmp_path)
        ctx = _context(runtime, session_id="s-read-vfs")
        await ctx.get_vfs_path("files/employees.csv").write_bytes(_SIMPLE_CSV)

        result = await ReadFileTool().execute({"path": "files/employees.csv"}, context=ctx)

        assert not result.isError, f"ReadFile failed: {result.text}"
        structured = result.structuredContent
        assert structured is not None
        assert structured["table_count"] == 1
        assert structured["total_rows"] == 3
        assert structured["tables"][0]["row_count"] == 3
        # The freshly-stored TabularDocument artifact id is exposed
        # so callers can fetch the snapshot back later.
        assert structured["tabular_artifact_id"]
        # When the input was a VFS path (not an artifact URI), the
        # primary artifact_id is the TabularDocument's id.
        assert structured["artifact_id"] == structured["tabular_artifact_id"]


@pytest.mark.unit
class TestReadFileArtifactURI:
    """Reading from a previously-stored artifact preserves its id."""

    async def test_read_file_via_artifact_uri_preserves_input_id(self, tmp_path: Path) -> None:
        runtime = _make_runtime(tmp_path)
        ctx = _context(runtime, session_id="s-read-art")
        manifest = await runtime.artifacts.create_from_bytes(
            _SIMPLE_CSV,
            context_id=ctx.session_id,
            session_id=ctx.session_id,
            name="employees.csv",
            mime_type="text/csv",
        )

        uri = f"kaos://artifacts/{manifest.artifact_id}"
        result = await ReadFileTool().execute({"path": uri}, context=ctx)

        assert not result.isError, f"ReadFile failed: {result.text}"
        structured = result.structuredContent
        assert structured is not None
        # The primary artifact_id echoes the *input* artifact id (the
        # bytes the user uploaded) so the SPA's ArtifactCard can render
        # the same handle the user already knows about. The newly
        # stored TabularDocument artifact remains addressable under
        # ``tabular_artifact_id``.
        assert structured["artifact_id"] == manifest.artifact_id
        assert structured["tabular_artifact_id"]
        assert structured["tabular_artifact_id"] != manifest.artifact_id


@pytest.mark.unit
class TestReadFileUnresolvable:
    """Missing VFS path surfaces a chip-friendly error."""

    async def test_read_file_missing_path_returns_three_part_error(self, tmp_path: Path) -> None:
        runtime = _make_runtime(tmp_path)
        ctx = _context(runtime, session_id="s-read-missing")

        result = await ReadFileTool().execute({"path": "files/does-not-exist.csv"}, context=ctx)

        assert result.isError
        text = result.text or ""
        assert "How to fix" in text
        assert "Alternative" in text
