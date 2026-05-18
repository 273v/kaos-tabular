"""Unit tests for kaos-tabular MCP tool layer.

Tests the KaosTool implementations in kaos_tabular.tools against a real
TabularEngine with test data. Tools that take a ``KaosContext`` await
``_get_engine(context)`` correctly; both async-execution paths and
metadata/annotation contracts are covered here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kaos_tabular.tools import (
    CountTool,
    DescribeTool,
    ExportTool,
    ListTablesTool,
    QueryTool,
    ReadFileTool,
    RegisterTool,
    SampleTool,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture()
def simple_csv(fixtures_dir: Path) -> Path:
    return fixtures_dir / "simple.csv"


# ---------------------------------------------------------------------------
# RegisterTool (the one tool that properly awaits _get_engine)
# ---------------------------------------------------------------------------


class TestRegisterTool:
    @pytest.mark.asyncio
    async def test_register_csv(self, simple_csv: Path) -> None:
        tool = RegisterTool()
        # URI contract redesign (kaos-core 0.1.0a10): file inputs must
        # carry an explicit scheme. Bare absolute paths are rejected at
        # the resolver. Use ``Path.as_uri()`` to produce a ``file:///``
        # URI.
        result = await tool.execute({"path": simple_csv.as_uri()})
        assert not result.isError
        structured = result.require_structured()
        assert structured["table_name"] == "simple"
        assert structured["row_count"] == 10
        assert structured["column_count"] >= 5

    @pytest.mark.asyncio
    async def test_register_custom_name(self, simple_csv: Path) -> None:
        tool = RegisterTool()
        result = await tool.execute({"path": simple_csv.as_uri(), "table_name": "my_table"})
        assert not result.isError
        assert result.require_structured()["table_name"] == "my_table"

    @pytest.mark.asyncio
    async def test_register_missing_file(self) -> None:
        tool = RegisterTool()
        # URI contract redesign (kaos-core 0.1.0a10): use a file:// URI
        # so the resolver reaches the filesystem-not-found branch rather
        # than rejecting the bare path at the scheme check.
        result = await tool.execute({"path": "file:///nonexistent/file.csv"})
        assert result.isError
        assert result.text is not None
        # After Stage 3 of vfs-blind-tools-audit-and-fix-plan.md the
        # resolver speaks in agent-friendly terms ("not found") rather
        # than the historical "File not found" phrase. The error must
        # still surface the missing path so the agent can self-correct.
        assert "not found" in result.text.lower()
        # url2pathname normalises to OS-native separators (Windows uses
        # backslashes), so check for the filename + parent dir tokens
        # independently of separator style.
        assert "nonexistent" in result.text
        assert "file.csv" in result.text

    @pytest.mark.asyncio
    async def test_metadata(self) -> None:
        tool = RegisterTool()
        meta = tool.metadata
        assert meta.name == "kaos-tabular-register"
        assert meta.annotations is not None
        assert meta.annotations.readOnlyHint is True


# ---------------------------------------------------------------------------
# Tool metadata and annotations
# ---------------------------------------------------------------------------


class TestToolMetadata:
    """Verify tool names follow the kaos-{module}-{action} pattern."""

    def test_register_tool_name(self) -> None:
        assert RegisterTool().metadata.name == "kaos-tabular-register"

    def test_query_tool_name(self) -> None:
        assert QueryTool().metadata.name == "kaos-tabular-query"

    def test_list_tables_tool_name(self) -> None:
        assert ListTablesTool().metadata.name == "kaos-tabular-list-tables"

    def test_describe_tool_name(self) -> None:
        assert DescribeTool().metadata.name == "kaos-tabular-describe"

    def test_sample_tool_name(self) -> None:
        assert SampleTool().metadata.name == "kaos-tabular-sample"

    def test_count_tool_name(self) -> None:
        assert CountTool().metadata.name == "kaos-tabular-count"

    def test_read_file_tool_name(self) -> None:
        assert ReadFileTool().metadata.name == "kaos-tabular-read-file"

    def test_export_tool_name(self) -> None:
        assert ExportTool().metadata.name == "kaos-tabular-export"


class TestToolAnnotations:
    """Verify every tool has explicit ToolAnnotations set."""

    def test_all_tools_have_annotations(self) -> None:
        tools = [
            RegisterTool(),
            QueryTool(),
            ListTablesTool(),
            DescribeTool(),
            SampleTool(),
            CountTool(),
            ReadFileTool(),
            ExportTool(),
        ]
        for tool in tools:
            meta = tool.metadata
            assert meta.annotations is not None, f"{meta.name} missing annotations"

    def test_export_is_not_readonly(self) -> None:
        tool = ExportTool()
        assert tool.metadata.annotations is not None
        assert tool.metadata.annotations.readOnlyHint is False

    def test_query_is_readonly(self) -> None:
        tool = QueryTool()
        assert tool.metadata.annotations is not None
        assert tool.metadata.annotations.readOnlyHint is True

    def test_register_is_readonly(self) -> None:
        tool = RegisterTool()
        assert tool.metadata.annotations is not None
        assert tool.metadata.annotations.readOnlyHint is True

    def test_list_tables_is_readonly(self) -> None:
        tool = ListTablesTool()
        assert tool.metadata.annotations is not None
        assert tool.metadata.annotations.readOnlyHint is True

    def test_describe_is_readonly(self) -> None:
        tool = DescribeTool()
        assert tool.metadata.annotations is not None
        assert tool.metadata.annotations.readOnlyHint is True


class TestToolInputSchema:
    """Verify tools have appropriate input schema."""

    def test_register_has_path_param(self) -> None:
        meta = RegisterTool().metadata
        param_names = [p.name for p in meta.input_schema]
        assert "path" in param_names

    def test_query_has_sql_param(self) -> None:
        meta = QueryTool().metadata
        param_names = [p.name for p in meta.input_schema]
        assert "sql" in param_names

    def test_describe_has_table_name(self) -> None:
        meta = DescribeTool().metadata
        param_names = [p.name for p in meta.input_schema]
        assert "table_name" in param_names

    def test_list_tables_no_required_params(self) -> None:
        meta = ListTablesTool().metadata
        assert meta.input_schema == []

    def test_export_has_format_constraint(self) -> None:
        meta = ExportTool().metadata
        fmt_param = next(p for p in meta.input_schema if p.name == "format")
        assert fmt_param.constraints is not None
        assert "enum" in fmt_param.constraints

    def test_query_has_max_rows_constraint(self) -> None:
        meta = QueryTool().metadata
        max_rows_param = next(p for p in meta.input_schema if p.name == "max_rows")
        assert max_rows_param.constraints is not None
        assert max_rows_param.constraints["max"] == 10000


class TestToolCount:
    """Verify the total number of tools matches expectations."""

    def test_register_tabular_tools_count(self) -> None:
        """register_tabular_tools should register exactly 17 tools.

        8 core + 6 reshape (history / find_duplicates / correlation /
        join / pivot / unpivot) + 3 structured shape (aggregate /
        filter / top_k).
        """
        from kaos_core import KaosRuntime

        from kaos_tabular.tools import (
            AggregateTool,
            CorrelationTool,
            FilterTool,
            FindDuplicatesTool,
            HistoryTool,
            JoinTool,
            PivotTool,
            TopKTool,
            UnpivotTool,
            register_tabular_tools,
        )

        tool_classes = [
            RegisterTool,
            QueryTool,
            ListTablesTool,
            DescribeTool,
            SampleTool,
            CountTool,
            ReadFileTool,
            ExportTool,
            HistoryTool,
            FindDuplicatesTool,
            CorrelationTool,
            JoinTool,
            PivotTool,
            UnpivotTool,
            AggregateTool,
            FilterTool,
            TopKTool,
        ]
        assert len(tool_classes) == 17
        assert register_tabular_tools(KaosRuntime()) == 17
