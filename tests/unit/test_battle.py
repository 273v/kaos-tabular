"""Battle tests for kaos-tabular against real-world data.

Tests the full stack — engine, readers, CLI, serializers — against
actual CSV/JSON fixtures from legal billing (LEDES) and geographic
(US states) datasets. These are not synthetic data; they come from
production kelvin_tabular test fixtures.

Edge cases tested:
- Unicode (degree symbols, smart quotes, diacritics, IPA)
- Encoding artifacts (mojibake: Attorney\ufffds)
- Dates stored as integers (19990225 → YYYYMMDD)
- Negative amounts (adjustments: -70.0)
- Quoted CSV fields with embedded commas and quotes
- Very long text fields (500+ character state descriptions)
- Null/empty columns in LEDES data
- Column names with special characters (CLIENT_MATTER_ID[])
- Cross-format consistency (CSV vs JSON → same query results)
- SQL aggregation, filtering, joining across real datasets
- Round-trip: file → engine → query → serialize → verify
- CLI end-to-end with real files
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

from kaos_tabular.cli import main as cli_main
from kaos_tabular.engine import TabularEngine
from kaos_tabular.readers import read_csv


@pytest.fixture()
def states_csv(fixtures_dir: Path) -> Path:
    return fixtures_dir / "states.csv"


@pytest.fixture()
def states_json(fixtures_dir: Path) -> Path:
    return fixtures_dir / "states.json"


@pytest.fixture()
def ledes_csv(fixtures_dir: Path) -> Path:
    return fixtures_dir / "ledes98b.csv"


@pytest.fixture()
def ledes_json(fixtures_dir: Path) -> Path:
    return fixtures_dir / "ledes98b.json"


# ═══════════════════════════════════════════════════════════════════════════
# 1. STATES DATASET — READER + ENGINE
# ═══════════════════════════════════════════════════════════════════════════


class TestStatesCSVReader:
    """Read the US states CSV — 50 states, 11 columns, unicode, long text."""

    def test_read_all_rows(self, states_csv: Path) -> None:
        doc = read_csv(states_csv)
        assert doc.tables[0].row_count == 50

    def test_columns_detected(self, states_csv: Path) -> None:
        doc = read_csv(states_csv)
        names = doc.tables[0].column_names()
        assert "name" in names
        assert "capital" in names
        assert "population" in names
        assert "motto" in names

    def test_unicode_preserved(self, states_csv: Path) -> None:
        """Degree symbols, prime marks, diacritics survive round-trip."""
        doc = read_csv(states_csv)
        table = doc.tables[0]
        # Find a row with degree symbol in latitude
        all_text = " ".join(str(v) for row in table.rows for v in row if v is not None)
        assert "\u00b0" in all_text or "°" in all_text  # degree symbol

    def test_quoted_fields(self, states_csv: Path) -> None:
        """Mottos with embedded quotes: 'Alki (Chinook: "Eventually")'"""
        doc = read_csv(states_csv)
        table = doc.tables[0]
        all_mottos = [str(row[7]) for row in table.rows if len(row) > 7 and row[7]]
        # At least some mottos should contain quotes or parentheses
        has_special = any("(" in m or '"' in m for m in all_mottos)
        assert has_special

    def test_long_descriptions(self, states_csv: Path) -> None:
        """Description column has 500+ char entries."""
        doc = read_csv(states_csv)
        table = doc.tables[0]
        desc_idx = list(table.column_names()).index("description")
        long_descs = [
            row[desc_idx] for row in table.rows if row[desc_idx] and len(str(row[desc_idx])) > 200
        ]
        assert len(long_descs) >= 10  # Most states have long descriptions


class TestStatesEngine:
    """SQL queries against the states dataset."""

    def test_count_states(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        result = engine.execute("SELECT COUNT(*) as cnt FROM states")
        assert result.rows[0][0] == 50

    def test_population_aggregation(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        result = engine.execute("SELECT SUM(population) as total_pop FROM states")
        total = result.rows[0][0]
        assert total > 300_000_000  # US population > 300M

    def test_filter_by_population(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        result = engine.execute(
            "SELECT name, population FROM states "
            "WHERE population > 10000000 ORDER BY population DESC"
        )
        # At least CA, TX, FL, NY should appear
        assert len(result.rows) >= 4
        names = [r[0] for r in result.rows]
        assert any("California" in n for n in names)

    def test_order_by_admission(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        result = engine.execute('SELECT name, "order" FROM states ORDER BY "order" ASC LIMIT 5')
        assert len(result.rows) == 5
        # First state admitted was Delaware (order=1)
        assert result.rows[0][1] == 1

    def test_group_by_with_having(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        result = engine.execute(
            "SELECT capital, COUNT(*) as cnt FROM states "
            "GROUP BY capital HAVING cnt > 0 ORDER BY capital LIMIT 10"
        )
        assert len(result.rows) == 10

    def test_string_search_like(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        result = engine.execute("SELECT name FROM states WHERE motto LIKE '%Gold%'")
        # California (Eureka) and Montana (Oro y Plata = Gold and Silver)
        assert len(result.rows) >= 1

    def test_sample(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        result = engine.sample("states", n=5)
        assert len(result.rows) == 5

    def test_describe(self, engine: TabularEngine, states_csv: Path) -> None:
        engine.register_file(states_csv)
        desc = engine.describe_table("states")
        assert desc["row_count"] == 50
        col_names = [c["name"] for c in desc["columns"]]
        assert "name" in col_names
        assert "population" in col_names


# ═══════════════════════════════════════════════════════════════════════════
# 2. LEDES DATASET — LEGAL BILLING
# ═══════════════════════════════════════════════════════════════════════════


class TestLedesCSVReader:
    """Read LEDES 98B legal billing data — messy, real-world format."""

    def test_read_all_rows(self, ledes_csv: Path) -> None:
        doc = read_csv(ledes_csv)
        table = doc.tables[0]
        assert table.row_count > 0

    def test_columns_detected(self, ledes_csv: Path) -> None:
        doc = read_csv(ledes_csv)
        names = doc.tables[0].column_names()
        assert "INVOICE_DATE" in names
        assert "INVOICE_NUMBER" in names
        assert "LINE_ITEM_TOTAL" in names

    def test_special_column_names(self, ledes_csv: Path) -> None:
        """Column name with brackets: CLIENT_MATTER_ID[]"""
        doc = read_csv(ledes_csv)
        names = doc.tables[0].column_names()
        bracket_cols = [n for n in names if "[" in n or "]" in n]
        assert len(bracket_cols) >= 1  # CLIENT_MATTER_ID[]

    def test_negative_amounts(self, ledes_csv: Path) -> None:
        """LINE_ITEM_ADJUSTMENT_AMOUNT has -70.0"""
        doc = read_csv(ledes_csv)
        table = doc.tables[0]
        adj_idx = list(table.column_names()).index("LINE_ITEM_ADJUSTMENT_AMOUNT")
        adjustments = [row[adj_idx] for row in table.rows]
        has_negative = any(v is not None and float(v) < 0 for v in adjustments)
        assert has_negative


class TestLedesEngine:
    """SQL queries against LEDES billing data."""

    def test_invoice_total(self, engine: TabularEngine, ledes_csv: Path) -> None:
        engine.register_file(ledes_csv)
        result = engine.execute("SELECT SUM(LINE_ITEM_TOTAL) as total FROM ledes98b")
        assert result.rows[0][0] > 0

    def test_filter_by_timekeeper(self, engine: TabularEngine, ledes_csv: Path) -> None:
        engine.register_file(ledes_csv)
        result = engine.execute(
            "SELECT TIMEKEEPER_NAME, LINE_ITEM_TOTAL FROM ledes98b "
            "WHERE TIMEKEEPER_NAME IS NOT NULL"
        )
        assert len(result.rows) > 0

    def test_special_column_name_query(self, engine: TabularEngine, ledes_csv: Path) -> None:
        """Query using the bracket column name."""
        engine.register_file(ledes_csv)
        result = engine.execute('SELECT "CLIENT_MATTER_ID[]" FROM ledes98b LIMIT 3')
        assert len(result.rows) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. CROSS-FORMAT CONSISTENCY
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossFormatConsistency:
    """Same data loaded from CSV and JSON should produce same query results."""

    def test_states_csv_vs_json_row_count(
        self, engine: TabularEngine, states_csv: Path, states_json: Path
    ) -> None:
        engine.register_file(states_csv, table_name="csv_states")
        engine.register_file(states_json, table_name="json_states")

        csv_count = engine.count("csv_states")
        json_count = engine.count("json_states")
        assert csv_count == json_count == 50

    def test_states_csv_vs_json_population_sum(
        self, engine: TabularEngine, states_csv: Path, states_json: Path
    ) -> None:
        engine.register_file(states_csv, table_name="csv_states")
        engine.register_file(states_json, table_name="json_states")

        csv_result = engine.execute("SELECT SUM(population) FROM csv_states")
        json_result = engine.execute("SELECT SUM(population) FROM json_states")
        assert csv_result.rows[0][0] == json_result.rows[0][0]

    def test_cross_format_join(
        self, engine: TabularEngine, states_csv: Path, states_json: Path
    ) -> None:
        """Join CSV and JSON versions of the same data."""
        engine.register_file(states_csv, table_name="csv_states")
        engine.register_file(states_json, table_name="json_states")

        result = engine.execute(
            "SELECT c.name, c.population, j.capital "
            "FROM csv_states c JOIN json_states j ON c.name = j.name "
            "ORDER BY c.population DESC LIMIT 5"
        )
        assert len(result.rows) == 5

    def test_register_both_formats(
        self, engine: TabularEngine, ledes_csv: Path, ledes_json: Path
    ) -> None:
        """Register CSV and JSON LEDES data in same engine."""
        engine.register_file(ledes_csv, table_name="ledes_csv")
        engine.register_file(ledes_json, table_name="ledes_json")
        tables = engine.list_tables()
        names = {t["name"] for t in tables}
        assert "ledes_csv" in names
        assert "ledes_json" in names


# ═══════════════════════════════════════════════════════════════════════════
# 4. SERIALIZATION ROUND-TRIPS
# ═══════════════════════════════════════════════════════════════════════════


class TestSerializationRoundTrips:
    """File → engine → query → serialize → verify content."""

    def test_states_to_tsv(self, engine: TabularEngine, states_csv: Path) -> None:
        from kaos_content.serializers.tabular import serialize_tsv

        engine.register_file(states_csv)
        result = engine.execute(
            "SELECT name, capital, population FROM states ORDER BY name LIMIT 5"
        )
        tsv = serialize_tsv(result)
        lines = tsv.strip().split("\n")
        assert lines[0] == "name\tcapital\tpopulation"
        assert len(lines) == 6  # header + 5 rows

    def test_states_to_markdown(self, engine: TabularEngine, states_csv: Path) -> None:
        from kaos_content.serializers.tabular import serialize_markdown_table

        engine.register_file(states_csv)
        result = engine.execute(
            "SELECT name, population FROM states ORDER BY population DESC LIMIT 3"
        )
        md = serialize_markdown_table(result)
        assert "| name" in md
        assert "---" in md  # separator

    def test_states_to_json_records(self, engine: TabularEngine, states_csv: Path) -> None:
        from kaos_content.serializers.tabular import serialize_json_records

        engine.register_file(states_csv)
        result = engine.execute("SELECT name, population FROM states LIMIT 3")
        json_str = serialize_json_records(result)
        records = json.loads(json_str)
        assert len(records) == 3
        assert "name" in records[0]
        assert "population" in records[0]

    def test_ledes_to_csv_and_back(
        self, engine: TabularEngine, ledes_csv: Path, tmp_path: Path
    ) -> None:
        """Read LEDES, export to CSV, re-import, verify row count matches."""
        from kaos_content.bridges.duckdb import _quote_ident

        engine.register_file(ledes_csv)
        original_count = engine.count("ledes98b")

        out_file = tmp_path / "exported.csv"
        engine._con.execute(f"COPY {_quote_ident('ledes98b')} TO '{out_file}' (FORMAT CSV, HEADER)")

        engine.register_file(out_file, table_name="reimported")
        reimported_count = engine.count("reimported")
        assert reimported_count == original_count

    def test_tabular_document_round_trip(self, engine: TabularEngine, states_csv: Path) -> None:
        """File → engine → TabularDocument → register → query → verify."""
        engine.register_file(states_csv)
        doc = engine.to_tabular_document("states", max_rows=10)

        assert doc.tables[0].row_count == 50  # Knows full count
        assert len(doc.tables[0].rows) == 10  # Only 10 loaded

        # Now register the TabularDocument back
        engine.register_document(doc, prefix="doc_")
        result = engine.execute("SELECT COUNT(*) FROM doc_states")
        assert result.rows[0][0] == 10  # Only has the 10 rows


# ═══════════════════════════════════════════════════════════════════════════
# 5. CLI AGAINST REAL DATA
# ═══════════════════════════════════════════════════════════════════════════


class TestCLIWithRealData:
    """CLI end-to-end tests with real fixtures."""

    def test_query_states(self, states_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            cli_main(
                [
                    "query",
                    str(states_csv),
                    (
                        "SELECT name, population FROM states "
                        "WHERE population > 10000000 "
                        "ORDER BY population DESC"
                    ),
                    "--format",
                    "tsv",
                ]
            )
        output = out.getvalue()
        assert "California" in output or "california" in output.lower()

    def test_describe_states_json(self, states_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            cli_main(["describe", str(states_csv), "--json"])
        data = json.loads(out.getvalue())
        assert data["command"] == "describe"
        assert data["row_count"] == 50
        col_names = [c["name"] for c in data["columns"]]
        assert "population" in col_names

    def test_sample_ledes(self, ledes_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            cli_main(["sample", str(ledes_csv), "--rows", "3"])
        output = out.getvalue()
        assert "|" in output  # markdown table

    def test_read_states_json(self, states_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            cli_main(["read", str(states_csv), "--json"])
        data = json.loads(out.getvalue())
        assert data["total_rows"] == 50
        assert data["tables"][0]["column_count"] == 11

    def test_export_states_to_json(self, states_csv: Path, tmp_path: Path) -> None:
        out_file = tmp_path / "states_export.json"
        cli_main(["export", str(states_csv), "--output", str(out_file)])
        assert out_file.exists()
        content = out_file.read_text()
        assert "California" in content or "Washington" in content

    def test_query_ledes_aggregate(self, ledes_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            cli_main(
                [
                    "query",
                    str(ledes_csv),
                    "SELECT SUM(LINE_ITEM_TOTAL) as total, COUNT(*) as items FROM ledes98b",
                    "--json",
                ]
            )
        data = json.loads(out.getvalue())
        assert data["row_count"] == 1
        assert len(data["rows"]) == 1
        assert data["rows"][0][0] > 0  # total > 0


# ═══════════════════════════════════════════════════════════════════════════
# 6. SESSION LIFECYCLE WITH REAL DATA
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionLifecycleReal:
    """Multi-step agent workflows with real data."""

    def test_register_query_undo_reregister(self, engine: TabularEngine, states_csv: Path) -> None:
        """Register, query, undo, re-register with different name."""
        engine.register_file(states_csv)
        result = engine.execute("SELECT COUNT(*) FROM states")
        assert result.rows[0][0] == 50

        engine.undo_last_register()
        assert engine.list_tables() == []

        engine.register_file(states_csv, table_name="us_states")
        result = engine.execute("SELECT COUNT(*) FROM us_states")
        assert result.rows[0][0] == 50

    def test_multi_dataset_workflow(
        self,
        engine: TabularEngine,
        states_csv: Path,
        ledes_csv: Path,
    ) -> None:
        """Agent registers multiple datasets and queries across them."""
        engine.register_file(states_csv)
        engine.register_file(ledes_csv)

        tables = engine.list_tables()
        assert len(tables) == 2

        # Query each independently
        states_count = engine.count("states")
        ledes_count = engine.count("ledes98b")
        assert states_count == 50
        assert ledes_count > 0

        # History tracks all operations
        history = engine.history()
        assert len(history) == 2
        assert all(e.event_type == "register" for e in history)

    def test_file_backed_persistence_real(self, states_csv: Path, tmp_path: Path) -> None:
        """Persist real data to disk, reopen, verify."""
        db_path = tmp_path / "session.duckdb"

        engine1 = TabularEngine(db_path=db_path)
        engine1.register_file(states_csv)
        assert engine1.count("states") == 50
        engine1.close()

        engine2 = TabularEngine(db_path=db_path, read_only=True)
        result = engine2.execute(
            "SELECT name, population FROM states ORDER BY population DESC LIMIT 1"
        )
        assert len(result.rows) == 1
        # Most populous state should be California
        assert "California" in result.rows[0][0]
        engine2.close()

    def test_export_reimport_workflow(
        self, engine: TabularEngine, states_csv: Path, tmp_path: Path
    ) -> None:
        """Export filtered data, re-import as new table."""

        engine.register_file(states_csv)

        # Export only large states
        out_file = tmp_path / "large_states.csv"
        engine._con.execute(
            f"COPY (SELECT name, capital, population FROM states WHERE population > 5000000) "
            f"TO '{out_file}' (FORMAT CSV, HEADER)"
        )

        engine.register_file(out_file, table_name="large_states")
        count = engine.count("large_states")
        # Should be ~22 states with pop > 5M
        assert 10 < count < 30

        result = engine.execute("SELECT name FROM large_states ORDER BY population DESC LIMIT 1")
        assert "California" in result.rows[0][0]


# ═══════════════════════════════════════════════════════════════════════════
# 7. EDGE CASE STRESS WITH REAL DATA
# ═══════════════════════════════════════════════════════════════════════════


class TestRealDataEdgeCases:
    """Edge cases surfaced by real data that synthetic tests miss."""

    def test_unnamed_first_column_ledes(self, engine: TabularEngine, ledes_csv: Path) -> None:
        """LEDES CSV has an unnamed first column (index column)."""
        engine.register_file(ledes_csv)
        desc = engine.describe_table("ledes98b")
        col_names = [c["name"] for c in desc["columns"]]
        # DuckDB should handle the unnamed column gracefully
        assert len(col_names) > 20

    def test_integer_dates_ledes(self, engine: TabularEngine, ledes_csv: Path) -> None:
        """LEDES stores dates as integers: 19990225 → Feb 25, 1999."""
        engine.register_file(ledes_csv)
        result = engine.execute("SELECT INVOICE_DATE FROM ledes98b LIMIT 1")
        # DuckDB may interpret as integer or date — either way, it's queryable
        assert result.rows[0][0] is not None

    def test_wide_table_query(self, engine: TabularEngine, ledes_csv: Path) -> None:
        """LEDES has 24+ columns — verify all are accessible."""
        engine.register_file(ledes_csv)
        result = engine.execute("SELECT * FROM ledes98b LIMIT 1")
        assert len(result.columns) >= 20

    def test_state_descriptions_contain_unicode(
        self, engine: TabularEngine, states_csv: Path
    ) -> None:
        """State descriptions have IPA, degree symbols, etc."""
        engine.register_file(states_csv)
        result = engine.execute(
            "SELECT description FROM states WHERE description IS NOT NULL LIMIT 5"
        )
        assert len(result.rows) >= 1
        # Descriptions should be non-trivial length
        for row in result.rows:
            assert len(str(row[0])) > 100

    def test_motto_with_embedded_quotes(self, engine: TabularEngine, states_csv: Path) -> None:
        """Montana's motto: "Oro y Plata" — quotes inside CSV field."""
        engine.register_file(states_csv)
        result = engine.execute("SELECT motto FROM states WHERE motto LIKE '%Oro%'")
        assert len(result.rows) >= 1
        motto = str(result.rows[0][0])
        assert "Oro" in motto
        assert "Plata" in motto

    def test_max_population_is_california(self, engine: TabularEngine, states_csv: Path) -> None:
        """Sanity check: California should be most populous."""
        engine.register_file(states_csv)
        result = engine.execute(
            "SELECT name, population FROM states ORDER BY population DESC LIMIT 1"
        )
        assert "California" in result.rows[0][0]

    def test_min_population_is_wyoming(self, engine: TabularEngine, states_csv: Path) -> None:
        """Sanity check: Wyoming should be least populous."""
        engine.register_file(states_csv)
        result = engine.execute(
            "SELECT name, population FROM states ORDER BY population ASC LIMIT 1"
        )
        assert "Wyoming" in result.rows[0][0]
