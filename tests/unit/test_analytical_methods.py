"""Unit coverage for the 5 analytical engine methods + their MCP tools.

Methods covered:
- ``TabularEngine.find_duplicates``  /  ``FindDuplicatesTool``
- ``TabularEngine.correlation``      /  ``CorrelationTool``
- ``TabularEngine.join``             /  ``JoinTool``
- ``TabularEngine.pivot``            /  ``PivotTool``
- ``TabularEngine.unpivot``          /  ``UnpivotTool``

The engine-side tests pin SQL behaviour against real DuckDB. The
tool-side tests confirm the MCP wrappers translate inputs faithfully
and emit the documented 3-part error shape on bad input.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from kaos_tabular.engine import TabularEngine
from kaos_tabular.errors import EngineError
from kaos_tabular.tools import (
    CorrelationTool,
    FindDuplicatesTool,
    HistoryTool,
    JoinTool,
    PivotTool,
    UnpivotTool,
)


def _write_csv(path: Path, headers: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(r)


@pytest.fixture()
def sales_engine(tmp_path: Path) -> TabularEngine:
    """Engine with a small 'sales' table including a deliberate duplicate."""
    csv_path = tmp_path / "sales.csv"
    _write_csv(
        csv_path,
        ["region", "product", "units", "price"],
        [
            ["east", "A", 10, 1.0],
            ["east", "A", 10, 1.0],  # dup of row 1
            ["east", "B", 5, 2.0],
            ["west", "A", 7, 1.0],
            ["west", "B", 3, 2.0],
        ],
    )
    eng = TabularEngine()
    eng.register_file(csv_path, table_name="sales")
    return eng


@pytest.fixture()
def regions_engine(sales_engine: TabularEngine, tmp_path: Path) -> TabularEngine:
    """sales_engine plus a 'regions' lookup table for join tests."""
    csv_path = tmp_path / "regions.csv"
    _write_csv(
        csv_path,
        ["region", "manager"],
        [["east", "Alice"], ["west", "Bob"]],
    )
    sales_engine.register_file(csv_path, table_name="regions")
    return sales_engine


# ---------------------------------------------------------------------------
# find_duplicates
# ---------------------------------------------------------------------------


class TestFindDuplicates:
    def test_default_columns_finds_full_row_dups(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            result = eng.find_duplicates("sales")
            assert result.row_count == 2  # the two identical east-A-10-1.0 rows

    def test_subset_columns_groups_more_loosely(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            result = eng.find_duplicates("sales", columns=["region", "product"])
            # (east, A) appears 2x; (east, B) 1x; (west, A) 1x; (west, B) 1x.
            assert result.row_count == 2

    def test_no_duplicates_returns_empty_table(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "uniq.csv"
        _write_csv(csv_path, ["x"], [[1], [2], [3]])
        with TabularEngine() as eng:
            eng.register_file(csv_path, table_name="uniq")
            result = eng.find_duplicates("uniq")
            assert result.row_count == 0

    def test_missing_column_raises_engine_error(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng, pytest.raises(EngineError) as exc_info:
            eng.find_duplicates("sales", columns=["does_not_exist"])
        msg = str(exc_info.value)
        assert "find_duplicates" in msg
        assert "describe_table" in msg


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------


class TestCorrelation:
    def test_auto_numeric_columns(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            result = eng.correlation("sales")
            # numeric columns are units + price → 2 x 2 = 4 rows
            assert result.row_count == 4
            assert [c.name for c in result.columns] == ["col_a", "col_b", "corr"]

    def test_one_column_raises(self, sales_engine: TabularEngine) -> None:
        """correlation requires at least two columns to make a pair."""
        with sales_engine as eng, pytest.raises(EngineError, match="at least 2 numeric"):
            eng.correlation("sales", columns=["units"])

    def test_explicit_columns_subset(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            result = eng.correlation("sales", columns=["units", "price"])
            assert result.row_count == 4  # 2 x 2 = 4 pairs
            # find the (units, units) row — should be 1.0 (self-correlation)
            cells = {(row[0], row[1]): row[2] for row in result.rows}
            assert cells[("units", "units")] == pytest.approx(1.0)
            assert cells[("price", "price")] == pytest.approx(1.0)
            # Symmetric
            assert cells[("units", "price")] == pytest.approx(cells[("price", "units")])


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


class TestJoin:
    def test_inner_join_on_shared_key(self, regions_engine: TabularEngine) -> None:
        with regions_engine as eng:
            result = eng.join("sales", "regions", on="region")
            # All 5 sales rows match a region — inner = 5
            assert result.row_count == 5
            cols = [c.name for c in result.columns]
            assert "manager" in cols
            assert cols.count("region") == 1  # USING dedups the join key

    def test_left_join_keeps_unmatched_left_rows(
        self, regions_engine: TabularEngine, tmp_path: Path
    ) -> None:
        # Add a south-region sale with no matching region.
        csv_path = tmp_path / "extra_sale.csv"
        _write_csv(csv_path, ["region", "product"], [["south", "C"]])
        with regions_engine as eng:
            eng.register_file(csv_path, table_name="extra")
            result = eng.join("extra", "regions", on="region", how="left")
            assert result.row_count == 1  # one extra row, manager=NULL

    def test_cross_join_matches_every_pair(self, regions_engine: TabularEngine) -> None:
        with regions_engine as eng:
            result = eng.join("sales", "regions", how="cross")
            assert result.row_count == 5 * 2  # 5 sales x 2 regions

    def test_target_materializes_and_registers(self, regions_engine: TabularEngine) -> None:
        with regions_engine as eng:
            eng.join("sales", "regions", on="region", target="sales_with_mgr")
            tables = {t["name"] for t in eng.list_tables()}
            assert "sales_with_mgr" in tables
            assert eng.count("sales_with_mgr") == 5

    def test_invalid_how_raises_engine_error(self, regions_engine: TabularEngine) -> None:
        with regions_engine as eng, pytest.raises(EngineError, match="not one of"):
            eng.join("sales", "regions", on="region", how="bogus")

    def test_cross_with_on_raises(self, regions_engine: TabularEngine) -> None:
        with regions_engine as eng, pytest.raises(EngineError, match="cross-joins have no key"):
            eng.join("sales", "regions", on="region", how="cross")

    def test_non_cross_without_on_raises(self, regions_engine: TabularEngine) -> None:
        with regions_engine as eng, pytest.raises(EngineError, match="requires on="):
            eng.join("sales", "regions")


# ---------------------------------------------------------------------------
# pivot / unpivot
# ---------------------------------------------------------------------------


class TestPivotUnpivot:
    def test_pivot_default_aggregate_is_sum(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            result = eng.pivot("sales", on="region", using="units")
            # No GROUP BY → 1 row, with columns one per distinct region.
            cols = [c.name for c in result.columns]
            assert "east" in cols
            assert "west" in cols

    def test_pivot_with_group_by(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            result = eng.pivot("sales", on="region", using="units", group_by="product")
            cols = [c.name for c in result.columns]
            assert "product" in cols
            assert "east" in cols and "west" in cols
            # 2 distinct products → 2 rows
            assert result.row_count == 2

    def test_pivot_invalid_aggregate_raises(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng, pytest.raises(EngineError, match="not one of"):
            eng.pivot("sales", on="region", using="units", aggregate="median")

    def test_unpivot_round_trip_via_pivot(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            eng.pivot(
                "sales",
                on="region",
                using="units",
                group_by="product",
                target="wide_units",
            )
            result = eng.unpivot("wide_units", columns=["east", "west"])
            cols = [c.name for c in result.columns]
            assert cols == ["product", "variable", "value"]
            # 2 products x 2 regions = 4 rows
            assert result.row_count == 4

    def test_unpivot_custom_name_value_columns(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng:
            eng.pivot(
                "sales",
                on="region",
                using="units",
                group_by="product",
                target="wide_units2",
            )
            result = eng.unpivot(
                "wide_units2",
                columns=["east", "west"],
                name_column="region_label",
                value_column="qty",
            )
            cols = [c.name for c in result.columns]
            assert "region_label" in cols
            assert "qty" in cols

    def test_unpivot_empty_columns_raises(self, sales_engine: TabularEngine) -> None:
        with sales_engine as eng, pytest.raises(EngineError, match="at least one"):
            eng.unpivot("sales", columns=[])


# ---------------------------------------------------------------------------
# MCP tool wrappers — smoke-level: verify they translate the engine output
# ---------------------------------------------------------------------------


class _MockContext:
    """Minimal KaosContext substitute for ephemeral-engine test paths."""

    session_id = ""


@pytest.mark.asyncio
async def test_history_tool_returns_recent_events(regions_engine: TabularEngine) -> None:
    # The bridge function `tools._get_engine` returns a fresh ephemeral
    # engine when context is None — that's a different instance from
    # `regions_engine`. Manually inject the warm engine instead by
    # populating the SESSION_REGISTRY.
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-history"] = regions_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-history"
        result = await HistoryTool().execute({"last_n": 5}, context=ctx)  # ty: ignore[invalid-argument-type]
        assert result.structuredContent is not None
        assert "events" in result.structuredContent
        assert result.structuredContent["count"] == len(result.structuredContent["events"])
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-history", None)


@pytest.mark.asyncio
async def test_find_duplicates_tool_routes_through_engine(
    regions_engine: TabularEngine,
) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-fd"] = regions_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-fd"
        result = await FindDuplicatesTool().execute(
            {"table_name": "sales"},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.structuredContent is not None
        assert result.structuredContent["duplicate_row_count"] == 2
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-fd", None)


@pytest.mark.asyncio
async def test_correlation_tool_routes_through_engine(
    regions_engine: TabularEngine,
) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-corr"] = regions_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-corr"
        result = await CorrelationTool().execute(
            {"table_name": "sales"},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.structuredContent is not None
        assert result.structuredContent["column_names"] == ["col_a", "col_b", "corr"]
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-corr", None)


@pytest.mark.asyncio
async def test_join_tool_routes_through_engine(regions_engine: TabularEngine) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-join"] = regions_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-join"
        result = await JoinTool().execute(
            {"left": "sales", "right": "regions", "on": ["region"], "how": "inner"},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.structuredContent is not None
        assert result.structuredContent["row_count"] == 5
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-join", None)


@pytest.mark.asyncio
async def test_join_tool_invalid_how_returns_error(
    regions_engine: TabularEngine,
) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-join-err"] = regions_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-join-err"
        result = await JoinTool().execute(
            {"left": "sales", "right": "regions", "on": ["region"], "how": "magic"},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.isError
        assert "not one of" in (result.text or "")
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-join-err", None)


@pytest.mark.asyncio
async def test_pivot_tool_routes_through_engine(sales_engine: TabularEngine) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-pivot"] = sales_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-pivot"
        result = await PivotTool().execute(
            {
                "table_name": "sales",
                "on": "region",
                "using": "units",
                "group_by": ["product"],
            },
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.structuredContent is not None
        assert result.structuredContent["row_count"] == 2
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-pivot", None)


@pytest.mark.asyncio
async def test_unpivot_tool_routes_through_engine(sales_engine: TabularEngine) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-unpivot"] = sales_engine
    try:
        sales_engine.pivot("sales", on="region", using="units", group_by="product", target="_w")
        ctx = _MockContext()
        ctx.session_id = "test-unpivot"
        result = await UnpivotTool().execute(
            {"table_name": "_w", "columns": ["east", "west"]},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.structuredContent is not None
        assert result.structuredContent["row_count"] == 4
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-unpivot", None)
