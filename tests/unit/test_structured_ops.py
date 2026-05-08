"""Unit coverage for the structured shape operations + their MCP tools.

Methods covered:
- ``TabularEngine.aggregate``  /  ``AggregateTool``
- ``TabularEngine.filter``     /  ``FilterTool``
- ``TabularEngine.top_k``      /  ``TopKTool``

Plus the did-you-mean machinery (``_suggestions`` + ``_did_you_mean_fragment``)
that backs every error message produced by the engine.

Tests anchor SQL behavior against real DuckDB (no mocks); the tool tests
confirm the MCP wrapper translates payload shapes into engine tuples
faithfully and surfaces the documented 3-part error shape on bad input.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from kaos_tabular.engine import (
    _AGGREGATE_FUNCTIONS,
    TabularEngine,
    _did_you_mean_fragment,
    _suggestions,
)
from kaos_tabular.errors import EngineError
from kaos_tabular.tools import (
    AggregateTool,
    FilterTool,
    TopKTool,
    _coerce_aggregates,
    _coerce_order_by,
)


def _write_csv(path: Path, headers: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(r)


@pytest.fixture()
def sales_engine(tmp_path: Path) -> TabularEngine:
    """Engine pre-loaded with a 'sales' table for aggregation/filtering tests."""
    csv_path = tmp_path / "sales.csv"
    _write_csv(
        csv_path,
        ["region", "product", "units", "price"],
        [
            ["east", "A", 10, 1.0],
            ["east", "B", 5, 2.0],
            ["east", "A", 8, 1.5],
            ["west", "A", 7, 1.0],
            ["west", "B", 3, 2.0],
            ["north", "A", 4, 1.2],
        ],
    )
    eng = TabularEngine()
    eng.register_file(csv_path, table_name="sales")
    return eng


# ---------------------------------------------------------------------------
# Did-you-mean helpers
# ---------------------------------------------------------------------------


class TestDidYouMean:
    """Cover ``_suggestions`` and ``_did_you_mean_fragment`` in isolation."""

    def test_empty_universe_returns_no_suggestions(self) -> None:
        assert _suggestions("anything", []) == []

    def test_single_typo_suggests_closest(self) -> None:
        # `units` (length 5) has 4 of 5 chars matching `unitss` — well
        # above the 0.6 cutoff.
        assert _suggestions("unitss", ["units", "price", "region"]) == ["units"]

    def test_no_close_match_returns_empty(self) -> None:
        # Two completely different identifiers must not match each
        # other under the 0.6 cutoff — guards against the "everything
        # matches everything" failure mode that hits short identifiers
        # with low cutoffs.
        assert _suggestions("zzzzzzzz", ["sales", "regions"]) == []

    def test_returns_at_most_n_suggestions(self) -> None:
        universe = ["sales", "saless", "saleses", "sale", "sals"]
        out = _suggestions("sales", universe, n=2)
        assert len(out) <= 2
        # `sales` is in the universe and is its own best match.
        assert out[0] == "sales"

    def test_fragment_empty_for_empty_matches(self) -> None:
        assert _did_you_mean_fragment([]) == ""

    def test_fragment_singular_form(self) -> None:
        assert _did_you_mean_fragment(["sales"]) == "Did you mean 'sales'?"

    def test_fragment_plural_form(self) -> None:
        f = _did_you_mean_fragment(["sales", "regions"])
        assert f.startswith("Did you mean one of:")
        assert "'sales'" in f
        assert "'regions'" in f


# ---------------------------------------------------------------------------
# engine.aggregate
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_grouped_two_aggregates_with_alias(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.aggregate(
            "sales",
            aggregates=[
                ("sum", "units", "total_units"),
                ("avg", "price"),
            ],
            group_by=["region"],
            order_by=[("region", "asc")],
        )
        assert result.row_count == 3
        # Column names: group_by columns + aggregate aliases / fallbacks
        names = [c.name for c in result.columns]
        assert names[0] == "region"
        assert "total_units" in names

    def test_no_group_by_collapses_to_single_row(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.aggregate(
            "sales",
            aggregates=[("count", "*", "row_count"), ("sum", "units", "total")],
        )
        assert result.row_count == 1
        row = result.rows[0]
        assert row[0] == 6  # six rows in fixture
        assert row[1] == sum([10, 5, 8, 7, 3, 4])

    def test_count_distinct(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.aggregate(
            "sales",
            aggregates=[("count_distinct", "region", "n_regions")],
        )
        assert result.rows[0][0] == 3

    def test_where_clause_filters_before_aggregate(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.aggregate(
            "sales",
            aggregates=[("sum", "units", "total")],
            group_by=["region"],
            where="product = 'A'",
            order_by=[("region", "asc")],
        )
        # Only product=A rows: east 10+8=18, north 4, west 7.
        rows = {r[0]: r[1] for r in result.rows}
        assert rows == {"east": 18, "north": 4, "west": 7}

    def test_having_filters_after_aggregate(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.aggregate(
            "sales",
            aggregates=[("sum", "units", "total")],
            group_by=["region"],
            having="total > 10",
        )
        # east total = 23, west total = 10, north total = 4
        # → only east makes the cut.
        assert result.row_count == 1
        assert result.rows[0][0] == "east"

    def test_having_without_group_by_rejected(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="HAVING requires group_by"):
            sales_engine.aggregate(
                "sales",
                aggregates=[("sum", "units", "total")],
                having="total > 100",
            )

    def test_order_by_aggregate_alias(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.aggregate(
            "sales",
            aggregates=[("sum", "units", "total")],
            group_by=["region"],
            order_by=[("total", "desc")],
        )
        totals = [r[1] for r in result.rows]
        assert totals == sorted(totals, reverse=True)

    def test_order_by_unaliased_aggregate_rejected(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="not a group_by column or aggregate alias"):
            sales_engine.aggregate(
                "sales",
                aggregates=[("sum", "units")],  # no alias
                group_by=["region"],
                order_by=[("total", "desc")],
            )

    def test_limit_caps_rows(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.aggregate(
            "sales",
            aggregates=[("sum", "units", "total")],
            group_by=["region"],
            order_by=[("total", "desc")],
            limit=1,
        )
        assert result.row_count == 1

    def test_target_materializes_result(self, sales_engine: TabularEngine) -> None:
        sales_engine.aggregate(
            "sales",
            aggregates=[("sum", "units", "total")],
            group_by=["region"],
            target="region_totals",
        )
        # Now queryable as a regular table.
        n = sales_engine.count("region_totals")
        assert n == 3

    # -- error paths with did-you-mean -------------------------------------

    def test_unknown_table_did_you_mean(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.aggregate(
                "saless",
                aggregates=[("sum", "units")],
            )
        msg = str(excinfo.value)
        assert "saless" in msg
        assert "Did you mean 'sales'" in msg

    def test_unknown_column_did_you_mean(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.aggregate(
                "sales",
                aggregates=[("sum", "unitss")],
            )
        msg = str(excinfo.value)
        assert "unitss" in msg
        assert "units" in msg

    def test_unknown_function_did_you_mean(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.aggregate(
                "sales",
                aggregates=[("summ", "units")],
            )
        msg = str(excinfo.value)
        assert "summ" in msg
        assert "Did you mean 'sum'" in msg

    def test_count_star_only_with_count(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="column='\\*' is only valid"):
            sales_engine.aggregate(
                "sales",
                aggregates=[("sum", "*")],
            )

    def test_empty_aggregates_rejected(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="at least one"):
            sales_engine.aggregate("sales", aggregates=[])

    def test_invalid_order_direction_rejected(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="direction must be"):
            sales_engine.aggregate(
                "sales",
                aggregates=[("sum", "units", "total")],
                group_by=["region"],
                order_by=[("total", "sideways")],
            )

    def test_supported_function_names_documented_in_error(
        self, sales_engine: TabularEngine
    ) -> None:
        # Sanity check: when the user passes a wrong func name, the
        # error message lists every supported func — agents shouldn't
        # have to guess what the whitelist is.
        with pytest.raises(EngineError) as excinfo:
            sales_engine.aggregate("sales", aggregates=[("totally_made_up", "units")])
        msg = str(excinfo.value)
        for f in _AGGREGATE_FUNCTIONS:
            assert f in msg


# ---------------------------------------------------------------------------
# engine.filter
# ---------------------------------------------------------------------------


class TestFilter:
    def test_simple_predicate(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.filter("sales", where="units > 5")
        assert result.row_count == 3  # 10, 8, 7

    def test_compound_predicate(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.filter(
            "sales",
            where="region = 'east' AND product = 'A'",
        )
        assert result.row_count == 2

    def test_limit_caps_rows(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.filter("sales", where="units > 0", limit=2)
        assert result.row_count == 2

    def test_target_materializes(self, sales_engine: TabularEngine) -> None:
        sales_engine.filter("sales", where="region = 'west'", target="west_only")
        assert sales_engine.count("west_only") == 2

    def test_empty_where_rejected(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="non-empty"):
            sales_engine.filter("sales", where="   ")

    def test_unknown_table_did_you_mean(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.filter("saless", where="units > 0")
        assert "Did you mean 'sales'" in str(excinfo.value)

    def test_bad_where_predicate_passes_through_duckdb_error(
        self, sales_engine: TabularEngine
    ) -> None:
        # Engine doesn't validate predicate columns — DuckDB does.
        # The message is still 3-part because we wrap.
        with pytest.raises(EngineError) as excinfo:
            sales_engine.filter("sales", where="not_a_real_column = 1")
        msg = str(excinfo.value)
        assert "filter failed" in msg
        assert "How to fix" in msg


# ---------------------------------------------------------------------------
# engine.top_k
# ---------------------------------------------------------------------------


class TestTopK:
    def test_default_descending(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.top_k("sales", by="units", n=3)
        units = [r[2] for r in result.rows]  # column order: region, product, units, price
        assert units == sorted(units, reverse=True)
        assert units[0] == 10

    def test_ascending(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.top_k("sales", by="units", n=2, ascending=True)
        units = [r[2] for r in result.rows]
        assert units == sorted(units)
        assert units[0] == 3  # smallest

    def test_multiple_by_columns(self, sales_engine: TabularEngine) -> None:
        # Sorts by region desc, then units desc
        result = sales_engine.top_k("sales", by=["region", "units"], n=10)
        regions = [r[0] for r in result.rows]
        # `west` > `north` > `east` lexicographically
        assert regions[0] == "west"
        assert regions[-1] == "east"

    def test_n_caps(self, sales_engine: TabularEngine) -> None:
        result = sales_engine.top_k("sales", by="units", n=2)
        assert result.row_count == 2

    def test_target_materializes(self, sales_engine: TabularEngine) -> None:
        sales_engine.top_k("sales", by="units", n=3, target="top3")
        assert sales_engine.count("top3") == 3

    def test_unknown_table_did_you_mean(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.top_k("saless", by="units")
        assert "Did you mean 'sales'" in str(excinfo.value)

    def test_unknown_column_did_you_mean(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.top_k("sales", by="unitss")
        msg = str(excinfo.value)
        assert "unitss" in msg
        assert "units" in msg

    def test_zero_n_rejected(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="must be >= 1"):
            sales_engine.top_k("sales", by="units", n=0)

    def test_empty_by_rejected(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError, match="at least one"):
            sales_engine.top_k("sales", by=[])


# ---------------------------------------------------------------------------
# Tool-payload coercion
# ---------------------------------------------------------------------------


class TestCoerceAggregates:
    def test_full_dict_with_alias(self) -> None:
        out = _coerce_aggregates([{"func": "sum", "column": "units", "alias": "total"}])
        assert out == [("sum", "units", "total")]

    def test_alias_optional(self) -> None:
        out = _coerce_aggregates([{"func": "avg", "column": "price"}])
        assert out == [("avg", "price", None)]

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(EngineError, match="non-empty list"):
            _coerce_aggregates([])

    def test_missing_func_rejected(self) -> None:
        with pytest.raises(EngineError, match="func must be"):
            _coerce_aggregates([{"column": "x"}])

    def test_missing_column_rejected(self) -> None:
        with pytest.raises(EngineError, match="column must be"):
            _coerce_aggregates([{"func": "sum"}])

    def test_non_object_entry_rejected(self) -> None:
        with pytest.raises(EngineError, match="must be an object"):
            _coerce_aggregates(["sum:units"])


class TestCoerceOrderBy:
    def test_full_dicts(self) -> None:
        out = _coerce_order_by([{"column": "total", "direction": "desc"}, {"column": "region"}])
        assert out == [("total", "desc"), ("region", "asc")]

    def test_none_passes_through(self) -> None:
        assert _coerce_order_by(None) is None

    def test_non_list_rejected(self) -> None:
        with pytest.raises(EngineError, match="list of"):
            _coerce_order_by("desc")


# ---------------------------------------------------------------------------
# MCP tool wrappers — translate inputs and surface engine errors
# ---------------------------------------------------------------------------


class _MockContext:
    session_id = ""


@pytest.mark.asyncio
async def test_aggregate_tool_grouped(sales_engine: TabularEngine) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-agg"] = sales_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-agg"
        result = await AggregateTool().execute(
            {
                "table_name": "sales",
                "aggregates": [
                    {"func": "sum", "column": "units", "alias": "total"},
                ],
                "group_by": ["region"],
                "order_by": [{"column": "total", "direction": "desc"}],
            },
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["row_count"] == 3
        assert "total" in result.structuredContent["column_names"]
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-agg", None)


@pytest.mark.asyncio
async def test_aggregate_tool_typo_returns_did_you_mean(
    sales_engine: TabularEngine,
) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-agg-err"] = sales_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-agg-err"
        result = await AggregateTool().execute(
            {
                "table_name": "saless",
                "aggregates": [{"func": "sum", "column": "units"}],
            },
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.isError is True
        assert result.content
        assert "Did you mean 'sales'" in result.content[0].text  # ty: ignore[unresolved-attribute]
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-agg-err", None)


@pytest.mark.asyncio
async def test_aggregate_tool_bad_payload_shape_surfaces_error(
    sales_engine: TabularEngine,
) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-agg-shape"] = sales_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-agg-shape"
        result = await AggregateTool().execute(
            {"table_name": "sales", "aggregates": []},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.isError is True
        assert "non-empty" in result.content[0].text  # ty: ignore[unresolved-attribute]
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-agg-shape", None)


@pytest.mark.asyncio
async def test_filter_tool_routes_to_engine(sales_engine: TabularEngine) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-flt"] = sales_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-flt"
        result = await FilterTool().execute(
            {"table_name": "sales", "where": "units > 5"},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["row_count"] == 3
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-flt", None)


@pytest.mark.asyncio
async def test_top_k_tool_routes_to_engine(sales_engine: TabularEngine) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-topk"] = sales_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-topk"
        result = await TopKTool().execute(
            {"table_name": "sales", "by": "units", "n": 2},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["row_count"] == 2
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-topk", None)


@pytest.mark.asyncio
async def test_top_k_tool_missing_by_returns_error(
    sales_engine: TabularEngine,
) -> None:
    from kaos_tabular import _session

    _session.SESSION_REGISTRY._engines["test-topk-err"] = sales_engine
    try:
        ctx = _MockContext()
        ctx.session_id = "test-topk-err"
        result = await TopKTool().execute(
            {"table_name": "sales", "n": 2},
            context=ctx,  # ty: ignore[invalid-argument-type]
        )
        assert result.isError is True
        assert "by= is required" in result.content[0].text  # ty: ignore[unresolved-attribute]
    finally:
        _session.SESSION_REGISTRY._engines.pop("test-topk-err", None)


# ---------------------------------------------------------------------------
# Did-you-mean retrofit — verify existing methods now suggest too
# ---------------------------------------------------------------------------


class TestExistingErrorPathsRetrofit:
    """Confirm the suggestion machinery is wired into pre-existing methods.

    The aggregate/filter/top_k changes wired ``_assert_table_exists`` /
    ``_assert_columns_exist`` into the existing analytical helpers.
    These tests pin that retrofit so a future refactor can't silently
    drop did-you-mean from the older surfaces.
    """

    def test_describe_table_typo_suggests(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.describe_table("saless")
        assert "Did you mean 'sales'" in str(excinfo.value)

    def test_count_typo_suggests(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.count("saless")
        assert "Did you mean 'sales'" in str(excinfo.value)

    def test_sample_typo_suggests(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.sample("saless")
        assert "Did you mean 'sales'" in str(excinfo.value)

    def test_pivot_typo_suggests(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.pivot("sales", on="prodct", using="units")
        msg = str(excinfo.value)
        assert "prodct" in msg
        assert "product" in msg

    def test_join_typo_suggests(self, sales_engine: TabularEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            sales_engine.join("saless", "sales", on="region")
        msg = str(excinfo.value)
        assert "saless" in msg
        assert "sales" in msg
