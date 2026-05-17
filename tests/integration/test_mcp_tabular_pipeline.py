"""Integration tests for kaos-tabular MCP tools.

Exercises the full MCP tool pipeline: register a CSV, query it with SQL,
list/describe/sample/count tables, and export results. All tools are invoked
via their ``execute()`` method with ``context=None`` (ephemeral engine mode).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from kaos_core import KaosContext

from kaos_tabular.tools import (
    CountTool,
    DescribeTool,
    ExportTool,
    ListTablesTool,
    QueryTool,
    RegisterTool,
    SampleTool,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_ROWS = [
    {"id": 1, "name": "Alice", "department": "Engineering", "salary": 95000.0},
    {"id": 2, "name": "Bob", "department": "Marketing", "salary": 72000.0},
    {"id": 3, "name": "Charlie", "department": "Engineering", "salary": 110000.0},
    {"id": 4, "name": "Diana", "department": "Sales", "salary": 68000.0},
    {"id": 5, "name": "Eve", "department": "Engineering", "salary": 102000.0},
    {"id": 6, "name": "Frank", "department": "Marketing", "salary": 78000.0},
    {"id": 7, "name": "Grace", "department": "Sales", "salary": 85000.0},
    {"id": 8, "name": "Hank", "department": "Engineering", "salary": 115000.0},
]


@pytest.fixture()
def test_csv(tmp_path: Path) -> Path:
    """Create a temporary CSV with employee test data."""
    csv_path = tmp_path / "employees.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "department", "salary"])
        writer.writeheader()
        writer.writerows(_TEST_ROWS)
    return csv_path


@pytest.fixture()
def test_json(tmp_path: Path) -> Path:
    """Create a temporary JSON file with the same employee data."""
    json_path = tmp_path / "employees.json"
    json_path.write_text(json.dumps(_TEST_ROWS))
    return json_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Tools are instantiated once per module; each test creates a fresh ephemeral
# engine because context=None always yields a new TabularEngine.
# However, the tools._ENGINES dict is keyed by session_id — with context=None
# the tool creates an ephemeral engine each call, so we need to share one
# engine across related calls within a single test. We achieve this by
# registering via the engine fixture from conftest and then calling the tool
# methods that share that same engine. Since the MCP tools use _get_engine()
# which creates ephemeral engines when context=None, we instead chain tool
# calls through a shared session by importing the engine management directly.

# For the integration pipeline we use the tools._ENGINES mechanism by passing
# a minimal context mock, OR we test the engine directly.
# The cleanest integration approach: use the TabularEngine for registration
# and then verify each tool's behaviour by calling execute() with context=None
# (which creates a fresh engine each time). For a true pipeline test we can
# chain calls on the same engine.

# Strategy: Test each tool independently first, then test a chained pipeline
# using a shared engine via a mock context.


def _context(session_id: str) -> KaosContext:
    """Create a real KaosContext with a stable session id for tool chaining."""
    return KaosContext.create(session_id=session_id)


# ---------------------------------------------------------------------------
# Tool tests: Register
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRegisterTool:
    """Tests for kaos-tabular-register."""

    async def test_register_csv(self, test_csv: Path) -> None:
        tool = RegisterTool()
        ctx = _context("register-csv")
        result = await tool.execute({"path": str(test_csv)}, context=ctx)

        assert not result.isError, f"Register failed: {result.text}"
        data = result.require_structured()
        assert data["table_name"] == "employees"
        assert data["row_count"] == 8
        assert data["column_count"] == 4
        assert any(c["name"] == "salary" for c in data["columns"])

    async def test_register_custom_name(self, test_csv: Path) -> None:
        tool = RegisterTool()
        ctx = _context("register-custom")
        result = await tool.execute(
            {"path": str(test_csv), "table_name": "staff"},
            context=ctx,
        )

        assert not result.isError
        data = result.require_structured()
        assert data["table_name"] == "staff"

    async def test_register_json(self, test_json: Path) -> None:
        tool = RegisterTool()
        ctx = _context("register-json")
        result = await tool.execute({"path": str(test_json)}, context=ctx)

        assert not result.isError
        data = result.require_structured()
        assert data["table_name"] == "employees"
        assert data["row_count"] == 8

    async def test_register_missing_file(self) -> None:
        tool = RegisterTool()
        result = await tool.execute({"path": "/nonexistent/data.csv"})

        assert result.isError
        text = result.text or ""
        # After Stage 3 of vfs-blind-tools-audit-and-fix-plan.md the
        # path resolver emits "not found" rather than the historical
        # "File not found" phrase; the path must still appear so the
        # agent can self-correct.
        assert "not found" in text.lower()
        assert "/nonexistent/data.csv" in text

    async def test_register_message_includes_guidance(self, test_csv: Path) -> None:
        tool = RegisterTool()
        ctx = _context("register-msg")
        result = await tool.execute({"path": str(test_csv)}, context=ctx)

        data = result.require_structured()
        assert "kaos-tabular-query" in data["message"]


# ---------------------------------------------------------------------------
# Tool tests: Query
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestQueryTool:
    """Tests for kaos-tabular-query."""

    async def test_basic_select(self, test_csv: Path) -> None:
        ctx = _context("query-basic")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await QueryTool().execute(
            {"sql": "SELECT * FROM employees"},
            context=ctx,
        )
        assert not result.isError, f"Query failed: {result.text}"
        text = result.require_text()
        assert "Alice" in text
        assert "8 rows" in text

    async def test_aggregation(self, test_csv: Path) -> None:
        ctx = _context("query-agg")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await QueryTool().execute(
            {
                "sql": (
                    "SELECT department, COUNT(*) as cnt, AVG(salary) as avg_salary "
                    "FROM employees GROUP BY department ORDER BY cnt DESC"
                )
            },
            context=ctx,
        )
        assert not result.isError
        text = result.require_text()
        # Engineering has 4 employees
        assert "Engineering" in text
        assert "3 rows" in text

    async def test_where_clause(self, test_csv: Path) -> None:
        ctx = _context("query-where")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await QueryTool().execute(
            {"sql": "SELECT name, salary FROM employees WHERE salary > 100000 ORDER BY salary"},
            context=ctx,
        )
        assert not result.isError
        text = result.require_text()
        assert "Charlie" in text
        assert "Eve" in text
        assert "Hank" in text
        # Bob (72k) should not appear
        assert "Bob" not in text

    async def test_query_no_tables_error(self) -> None:
        ctx = _context("query-empty")
        result = await QueryTool().execute(
            {"sql": "SELECT 1"},
            context=ctx,
        )
        assert result.isError
        assert "No tables registered" in (result.text or "")

    async def test_query_bad_sql_error(self, test_csv: Path) -> None:
        ctx = _context("query-bad-sql")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await QueryTool().execute(
            {"sql": "SELECT nonexistent_col FROM employees"},
            context=ctx,
        )
        assert result.isError
        assert "SQL error" in (result.text or "")

    async def test_max_rows_limit(self, test_csv: Path) -> None:
        ctx = _context("query-limit")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await QueryTool().execute(
            {"sql": "SELECT * FROM employees", "max_rows": 3},
            context=ctx,
        )
        assert not result.isError
        text = result.require_text()
        assert "3 rows" in text


# ---------------------------------------------------------------------------
# Tool tests: List Tables
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestListTablesTool:
    """Tests for kaos-tabular-list-tables."""

    async def test_empty_list(self) -> None:
        ctx = _context("list-empty")
        result = await ListTablesTool().execute({}, context=ctx)

        assert not result.isError
        data = result.require_structured()
        assert data["count"] == 0
        assert data["tables"] == []

    async def test_list_after_register(self, test_csv: Path) -> None:
        ctx = _context("list-one")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await ListTablesTool().execute({}, context=ctx)
        assert not result.isError
        data = result.require_structured()
        assert data["count"] == 1
        tables = data["tables"]
        assert len(tables) == 1
        assert tables[0]["name"] == "employees"

    async def test_list_multiple_tables(
        self, test_csv: Path, test_json: Path, tmp_path: Path
    ) -> None:
        ctx = _context("list-multi")
        reg = RegisterTool()
        await reg.execute({"path": str(test_csv), "table_name": "csv_data"}, context=ctx)

        # Create a second CSV with different data
        second_csv = tmp_path / "departments.csv"
        with second_csv.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["dept", "budget"])
            writer.writeheader()
            writer.writerows(
                [
                    {"dept": "Engineering", "budget": 500000},
                    {"dept": "Marketing", "budget": 200000},
                ]
            )
        await reg.execute({"path": str(second_csv), "table_name": "budgets"}, context=ctx)

        result = await ListTablesTool().execute({}, context=ctx)
        data = result.require_structured()
        assert data["count"] == 2
        names = {t["name"] for t in data["tables"]}
        assert "csv_data" in names
        assert "budgets" in names


# ---------------------------------------------------------------------------
# Tool tests: Describe
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDescribeTool:
    """Tests for kaos-tabular-describe."""

    async def test_describe_schema(self, test_csv: Path) -> None:
        ctx = _context("describe-schema")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await DescribeTool().execute(
            {"table_name": "employees"},
            context=ctx,
        )
        assert not result.isError
        data = result.require_structured()
        assert data["row_count"] == 8
        assert data["column_count"] == 4

        col_names = [c["name"] for c in data["columns"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "department" in col_names
        assert "salary" in col_names

    async def test_describe_nonexistent_table(self, test_csv: Path) -> None:
        ctx = _context("describe-missing")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await DescribeTool().execute(
            {"table_name": "nonexistent"},
            context=ctx,
        )
        assert result.isError
        assert "not found" in (result.text or "").lower()


# ---------------------------------------------------------------------------
# Tool tests: Sample
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSampleTool:
    """Tests for kaos-tabular-sample."""

    async def test_sample_default(self, test_csv: Path) -> None:
        ctx = _context("sample-default")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await SampleTool().execute(
            {"table_name": "employees"},
            context=ctx,
        )
        assert not result.isError
        text = result.require_text()
        # Markdown table should contain pipe separators and column headers
        assert "|" in text
        assert "name" in text.lower() or "id" in text.lower()

    async def test_sample_custom_n(self, test_csv: Path) -> None:
        ctx = _context("sample-n")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await SampleTool().execute(
            {"table_name": "employees", "n": 3},
            context=ctx,
        )
        assert not result.isError
        text = result.require_text()
        # Count data rows (lines with pipes, excluding header and separator)
        data_lines = [
            line for line in text.strip().split("\n") if "|" in line and "---" not in line
        ]
        # Header line + 3 data rows = 4 lines with pipes
        assert len(data_lines) == 4  # header + 3 data rows

    async def test_sample_nonexistent_table(self) -> None:
        ctx = _context("sample-missing")
        result = await SampleTool().execute(
            {"table_name": "nonexistent"},
            context=ctx,
        )
        assert result.isError


# ---------------------------------------------------------------------------
# Tool tests: Count
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCountTool:
    """Tests for kaos-tabular-count."""

    async def test_count(self, test_csv: Path) -> None:
        ctx = _context("count-basic")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        result = await CountTool().execute(
            {"table_name": "employees"},
            context=ctx,
        )
        assert not result.isError
        data = result.require_structured()
        assert data["table_name"] == "employees"
        assert data["row_count"] == 8

    async def test_count_nonexistent(self) -> None:
        ctx = _context("count-missing")
        result = await CountTool().execute(
            {"table_name": "nonexistent"},
            context=ctx,
        )
        assert result.isError
        assert "nonexistent" in (result.text or "")


# ---------------------------------------------------------------------------
# Tool tests: Export
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExportTool:
    """Tests for kaos-tabular-export."""

    async def test_export_csv(self, test_csv: Path, tmp_path: Path) -> None:
        ctx = _context("export-csv")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        output_path = tmp_path / "output.csv"
        result = await ExportTool().execute(
            {"table_name": "employees", "output_path": str(output_path)},
            context=ctx,
        )
        assert not result.isError, f"Export failed: {result.text}"
        data = result.require_structured()
        assert data["row_count"] == 8
        assert data["format"] == "csv"

        # Verify the exported file exists and has the right content
        assert output_path.exists()
        lines = output_path.read_text().strip().split("\n")
        assert len(lines) == 9  # header + 8 data rows

    async def test_export_json(self, test_csv: Path, tmp_path: Path) -> None:
        ctx = _context("export-json")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        output_path = tmp_path / "output.json"
        result = await ExportTool().execute(
            {"table_name": "employees", "output_path": str(output_path)},
            context=ctx,
        )
        assert not result.isError
        data = result.require_structured()
        assert data["format"] == "json"

        # DuckDB exports NDJSON (one JSON object per line), not a JSON array
        assert output_path.exists()
        lines = [
            json.loads(line) for line in output_path.read_text().strip().split("\n") if line.strip()
        ]
        assert len(lines) == 8
        assert all("name" in row for row in lines)

    async def test_export_parquet(self, test_csv: Path, tmp_path: Path) -> None:
        ctx = _context("export-parquet")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        output_path = tmp_path / "output.parquet"
        result = await ExportTool().execute(
            {"table_name": "employees", "output_path": str(output_path)},
            context=ctx,
        )
        assert not result.isError
        data = result.require_structured()
        assert data["format"] == "parquet"
        assert output_path.exists()
        assert output_path.stat().st_size > 0

    async def test_export_unknown_format(self, test_csv: Path, tmp_path: Path) -> None:
        ctx = _context("export-bad-fmt")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        output_path = tmp_path / "output.xyz"
        result = await ExportTool().execute(
            {"table_name": "employees", "output_path": str(output_path)},
            context=ctx,
        )
        assert result.isError
        # Audit-01 KTAB-007 rewrote the error to a 3-part shape:
        # "Cannot infer export format from extension '.xyz'. How to
        # fix: ... Alternative: ...". Match the new prefix.
        assert "Cannot infer export format" in (result.text or "")

    async def test_export_explicit_format(self, test_csv: Path, tmp_path: Path) -> None:
        ctx = _context("export-explicit")
        await RegisterTool().execute({"path": str(test_csv)}, context=ctx)

        output_path = tmp_path / "data_export"
        result = await ExportTool().execute(
            {
                "table_name": "employees",
                "output_path": str(output_path),
                "format": "csv",
            },
            context=ctx,
        )
        assert not result.isError
        assert output_path.exists()


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFullPipeline:
    """End-to-end pipeline: register -> list -> describe -> query -> sample -> count -> export."""

    async def test_complete_pipeline(self, test_csv: Path, tmp_path: Path) -> None:
        ctx = _context("pipeline-full")

        # 1. Register
        reg_result = await RegisterTool().execute(
            {"path": str(test_csv)},
            context=ctx,
        )
        assert not reg_result.isError
        reg_data = reg_result.require_structured()
        assert reg_data["table_name"] == "employees"
        assert reg_data["row_count"] == 8

        # 2. List tables
        list_result = await ListTablesTool().execute({}, context=ctx)
        assert not list_result.isError
        list_data = list_result.require_structured()
        assert list_data["count"] == 1
        assert list_data["tables"][0]["name"] == "employees"

        # 3. Describe
        desc_result = await DescribeTool().execute(
            {"table_name": "employees"},
            context=ctx,
        )
        assert not desc_result.isError
        desc_data = desc_result.require_structured()
        assert desc_data["row_count"] == 8
        assert desc_data["column_count"] == 4
        col_names = {c["name"] for c in desc_data["columns"]}
        assert col_names == {"id", "name", "department", "salary"}

        # 4. Query -- find high earners
        query_result = await QueryTool().execute(
            {
                "sql": (
                    "SELECT name, salary FROM employees WHERE salary > 100000 ORDER BY salary DESC"
                )
            },
            context=ctx,
        )
        assert not query_result.isError
        query_text = query_result.require_text()
        assert "Hank" in query_text
        assert "Charlie" in query_text
        assert "Eve" in query_text

        # 5. Query -- department aggregation
        agg_result = await QueryTool().execute(
            {
                "sql": (
                    "SELECT department, COUNT(*) as headcount, "
                    "ROUND(AVG(salary), 2) as avg_salary "
                    "FROM employees GROUP BY department ORDER BY headcount DESC"
                )
            },
            context=ctx,
        )
        assert not agg_result.isError
        agg_text = agg_result.require_text()
        assert "Engineering" in agg_text

        # 6. Sample
        sample_result = await SampleTool().execute(
            {"table_name": "employees", "n": 3},
            context=ctx,
        )
        assert not sample_result.isError
        sample_text = sample_result.require_text()
        assert "|" in sample_text  # markdown table

        # 7. Count
        count_result = await CountTool().execute(
            {"table_name": "employees"},
            context=ctx,
        )
        assert not count_result.isError
        count_data = count_result.require_structured()
        assert count_data["row_count"] == 8

        # 8. Export to CSV
        export_path = tmp_path / "pipeline_output.csv"
        export_result = await ExportTool().execute(
            {"table_name": "employees", "output_path": str(export_path)},
            context=ctx,
        )
        assert not export_result.isError
        assert export_path.exists()
        exported_lines = export_path.read_text().strip().split("\n")
        assert len(exported_lines) == 9  # header + 8 rows

    async def test_pipeline_with_multiple_tables(self, tmp_path: Path) -> None:
        """Register two tables, then run a cross-table join query."""
        ctx = _context("pipeline-join")

        # Create employees CSV
        emp_csv = tmp_path / "emp.csv"
        with emp_csv.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "name", "dept_id"])
            writer.writeheader()
            writer.writerows(
                [
                    {"id": 1, "name": "Alice", "dept_id": 10},
                    {"id": 2, "name": "Bob", "dept_id": 20},
                    {"id": 3, "name": "Charlie", "dept_id": 10},
                ]
            )

        # Create departments CSV
        dept_csv = tmp_path / "dept.csv"
        with dept_csv.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["dept_id", "dept_name"])
            writer.writeheader()
            writer.writerows(
                [
                    {"dept_id": 10, "dept_name": "Engineering"},
                    {"dept_id": 20, "dept_name": "Marketing"},
                ]
            )

        # Register both
        reg = RegisterTool()
        r1 = await reg.execute({"path": str(emp_csv)}, context=ctx)
        assert not r1.isError
        r2 = await reg.execute({"path": str(dept_csv)}, context=ctx)
        assert not r2.isError

        # Verify two tables listed
        list_result = await ListTablesTool().execute({}, context=ctx)
        assert list_result.require_structured()["count"] == 2

        # Cross-table join
        join_result = await QueryTool().execute(
            {
                "sql": (
                    "SELECT e.name, d.dept_name "
                    "FROM emp e JOIN dept d ON e.dept_id = d.dept_id "
                    "ORDER BY e.name"
                )
            },
            context=ctx,
        )
        assert not join_result.isError
        text = join_result.require_text()
        assert "Alice" in text
        assert "Engineering" in text
        assert "Bob" in text
        assert "Marketing" in text


# ---------------------------------------------------------------------------
# Tool metadata validation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestToolMetadata:
    """Verify tool metadata follows KAOS conventions."""

    @pytest.fixture()
    def all_tools(
        self,
    ) -> list[
        RegisterTool
        | QueryTool
        | ListTablesTool
        | DescribeTool
        | SampleTool
        | CountTool
        | ExportTool
    ]:
        return [
            RegisterTool(),
            QueryTool(),
            ListTablesTool(),
            DescribeTool(),
            SampleTool(),
            CountTool(),
            ExportTool(),
        ]

    def test_all_tools_have_annotations(self, all_tools: list) -> None:
        for tool in all_tools:
            meta = tool.metadata
            assert meta.annotations is not None, f"{meta.name} missing ToolAnnotations"

    def test_all_tools_follow_naming_convention(self, all_tools: list) -> None:
        for tool in all_tools:
            name = tool.metadata.name
            assert name.startswith("kaos-tabular-"), (
                f"{name} does not follow kaos-{{module}}-{{action}} pattern"
            )
            parts = name.split("-")
            assert len(parts) >= 3, f"{name} must have at least 3 segments"

    def test_export_is_not_readonly(self) -> None:
        meta = ExportTool().metadata
        assert meta.annotations is not None
        assert meta.annotations.readOnlyHint is False

    def test_query_tools_are_readonly(self) -> None:
        readonly_tools = [QueryTool(), ListTablesTool(), DescribeTool(), SampleTool(), CountTool()]
        for tool in readonly_tools:
            meta = tool.metadata
            assert meta.annotations is not None
            assert meta.annotations.readOnlyHint is True, f"{meta.name} should be readOnlyHint=True"
