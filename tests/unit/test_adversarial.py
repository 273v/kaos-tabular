"""Adversarial and stress tests for kaos-tabular.

Tests the engine against:
- Size: 100K+ rows, 200+ columns, multi-MB files
- Bad data: SQL injection, path traversal, malformed CSV, binary garbage
- Pathological patterns: all-null tables, single-value columns, enormous cells
- Performance: benchmarks with wall-clock assertions
- Memory: verify we don't load entire tables into RAM for queries
- Concurrency: multiple engines, sequential heavy queries
"""

from __future__ import annotations

import csv
import os
import time
from io import StringIO
from pathlib import Path
from unittest import mock

import duckdb
import pytest

from kaos_tabular.cli import main as cli_main
from kaos_tabular.engine import TabularEngine
from kaos_tabular.readers import read_csv

# ═══════════════════════════════════════════════════════════════════════════
# HELPERS — Generate test data files
# ═══════════════════════════════════════════════════════════════════════════


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def _generate_large_csv(path: Path, n_rows: int, n_cols: int = 5) -> None:
    """Generate a CSV with n_rows rows and n_cols columns."""
    headers = [f"col_{i}" for i in range(n_cols)]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i in range(n_rows):
            w.writerow([str(i * n_cols + j) for j in range(n_cols)])


def _generate_wide_csv(path: Path, n_cols: int, n_rows: int = 10) -> None:
    """Generate a CSV with many columns."""
    headers = [f"c{i:04d}" for i in range(n_cols)]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i in range(n_rows):
            w.writerow([str(i) for _ in range(n_cols)])


# ═══════════════════════════════════════════════════════════════════════════
# 1. SIZE STRESS — ROWS
# ═══════════════════════════════════════════════════════════════════════════


class TestLargeRowCounts:
    """Verify the engine handles large datasets without choking."""

    def test_100k_rows_register_and_count(self, tmp_path: Path) -> None:
        """100,000 rows — register + count should complete in < 5s."""
        csv_file = tmp_path / "large.csv"
        _generate_large_csv(csv_file, n_rows=100_000)

        start = time.monotonic()
        with TabularEngine() as engine:
            engine.register_file(csv_file)
            count = engine.count("large")
            elapsed = time.monotonic() - start

        assert count == 100_000
        assert elapsed < 5.0, f"100K row registration took {elapsed:.2f}s (limit: 5s)"

    def test_100k_rows_aggregate_query(self, tmp_path: Path) -> None:
        """Aggregate over 100K rows should be fast (DuckDB vectorized)."""
        csv_file = tmp_path / "agg.csv"
        _generate_large_csv(csv_file, n_rows=100_000)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="data")

            start = time.monotonic()
            result = engine.execute(
                "SELECT COUNT(*), SUM(CAST(col_0 AS BIGINT)), AVG(CAST(col_1 AS DOUBLE)) FROM data"
            )
            elapsed = time.monotonic() - start

        assert result.rows[0][0] == 100_000
        assert elapsed < 2.0, f"100K aggregate took {elapsed:.2f}s (limit: 2s)"

    def test_100k_rows_filtered_query(self, tmp_path: Path) -> None:
        """WHERE clause over 100K rows."""
        csv_file = tmp_path / "filter.csv"
        _generate_large_csv(csv_file, n_rows=100_000)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="data")

            start = time.monotonic()
            result = engine.execute(
                "SELECT * FROM data WHERE CAST(col_0 AS INTEGER) < 50", max_rows=100
            )
            elapsed = time.monotonic() - start

        assert len(result.rows) <= 100
        assert elapsed < 2.0

    def test_max_rows_cap_enforced(self, tmp_path: Path) -> None:
        """Even with 100K rows, result is capped at max_rows."""
        csv_file = tmp_path / "cap.csv"
        _generate_large_csv(csv_file, n_rows=100_000)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="data")
            result = engine.execute("SELECT * FROM data", max_rows=50)
            assert len(result.rows) == 50

    def test_reader_100k_rows(self, tmp_path: Path) -> None:
        """read_csv on 100K rows returns TabularDocument."""
        csv_file = tmp_path / "reader_large.csv"
        _generate_large_csv(csv_file, n_rows=100_000, n_cols=3)

        start = time.monotonic()
        doc = read_csv(csv_file)
        elapsed = time.monotonic() - start

        assert doc.tables[0].row_count == 100_000
        assert elapsed < 10.0, f"read_csv 100K took {elapsed:.2f}s (limit: 10s)"


# ═══════════════════════════════════════════════════════════════════════════
# 2. SIZE STRESS — COLUMNS
# ═══════════════════════════════════════════════════════════════════════════


class TestWideTablesStress:
    """Verify the engine handles very wide tables."""

    def test_200_columns(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "wide.csv"
        _generate_wide_csv(csv_file, n_cols=200)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="wide")
            desc = engine.describe_table("wide")
            assert desc["column_count"] == 200

    def test_200_columns_select_star(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "wide2.csv"
        _generate_wide_csv(csv_file, n_cols=200, n_rows=5)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="wide")
            result = engine.execute("SELECT * FROM wide")
            assert len(result.columns) == 200
            assert len(result.rows) == 5

    def test_200_columns_specific_select(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "wide3.csv"
        _generate_wide_csv(csv_file, n_cols=200, n_rows=5)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="wide")
            result = engine.execute("SELECT c0000, c0099, c0199 FROM wide")
            assert len(result.columns) == 3


# ═══════════════════════════════════════════════════════════════════════════
# 3. SIZE STRESS — FILE SIZE
# ═══════════════════════════════════════════════════════════════════════════


class TestLargeFileSize:
    """Test with multi-MB files."""

    def test_5mb_csv(self, tmp_path: Path) -> None:
        """Generate ~5MB CSV, register, query."""
        csv_file = tmp_path / "big.csv"
        # 100K rows x 10 cols of ~5-char values ≈ 5MB
        _generate_large_csv(csv_file, n_rows=100_000, n_cols=10)
        file_size = csv_file.stat().st_size
        assert file_size > 3_000_000  # at least 3MB

        with TabularEngine() as engine:
            engine.register_file(csv_file)
            count = engine.count("big")
            assert count == 100_000

    def test_large_cell_values(self, tmp_path: Path) -> None:
        """Cells with 10KB+ text (legal document paragraphs)."""
        csv_file = tmp_path / "bigcells.csv"
        big_text = "x" * 10_000  # 10KB per cell
        _write_csv(csv_file, ["id", "text"], [["1", big_text], ["2", big_text]])

        with TabularEngine() as engine:
            engine.register_file(csv_file)
            result = engine.execute("SELECT LENGTH(text) FROM bigcells")
            assert result.rows[0][0] == 10_000


# ═══════════════════════════════════════════════════════════════════════════
# 4. SQL INJECTION / ADVERSARIAL SQL
# ═══════════════════════════════════════════════════════════════════════════


class TestSQLInjection:
    """Verify SQL injection attempts don't cause harm."""

    def test_drop_table_in_query(self, engine: TabularEngine, simple_csv: Path) -> None:
        """DROP TABLE should fail — engine is not read-only but DuckDB limits damage."""
        engine.register_file(simple_csv)
        # This wraps in SELECT * FROM (...) LIMIT N, so DROP inside fails
        with pytest.raises(duckdb.Error):
            engine.execute("DROP TABLE simple; SELECT 1")

    def test_semicolon_injection(self, engine: TabularEngine, simple_csv: Path) -> None:
        """Semicolons in SQL — DuckDB should reject multi-statement."""
        engine.register_file(simple_csv)
        with pytest.raises(duckdb.Error):
            engine.execute("SELECT 1; SELECT 2")

    def test_file_read_via_sql(self, engine: TabularEngine, tmp_path: Path) -> None:
        """DuckDB can read files via SQL — this is by design (in-process engine).
        Security boundary is at the MCP tool layer, not the SQL engine."""
        # DuckDB has same filesystem access as the Python process
        # This test documents the behavior, not prevents it
        readable = tmp_path / "readable.csv"
        _write_csv(readable, ["a"], [["1"], ["2"]])
        result = engine.execute(f"SELECT COUNT(*) FROM read_csv_auto('{readable}')")
        assert result.rows[0][0] == 2

    def test_table_name_injection(self, engine: TabularEngine, tmp_path: Path) -> None:
        """Table name with SQL injection attempt."""
        evil_csv = tmp_path / "evil.csv"
        _write_csv(evil_csv, ["x"], [["1"]])
        # Register with a malicious name — should be quoted
        name = engine.register_file(evil_csv, table_name="evil'; DROP TABLE users; --")
        # The table should be queryable (name is quoted)
        count = engine.count(name)
        assert count == 1


# ═══════════════════════════════════════════════════════════════════════════
# 5. PATH TRAVERSAL
# ═══════════════════════════════════════════════════════════════════════════


class TestPathTraversal:
    """Verify path traversal attempts are handled safely."""

    def test_relative_path_traversal(self, engine: TabularEngine) -> None:
        with pytest.raises(FileNotFoundError):
            engine.register_file("../../../__kaos_tabular_missing__/passwd.csv")

    def test_null_byte_in_path(self, engine: TabularEngine) -> None:
        with pytest.raises((FileNotFoundError, ValueError, OSError)):
            engine.register_file("/tmp/evil\x00.csv")

    def test_symlink_follows_target(self, engine: TabularEngine, tmp_path: Path) -> None:
        """Symlink resolves to target — DuckDB reads the real file."""
        link = tmp_path / "link.csv"
        target = tmp_path / "real.csv"
        _write_csv(target, ["x"], [["1"]])
        link.symlink_to(target)
        name = engine.register_file(link, table_name="linked")
        assert engine.count(name) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. MALFORMED / GARBAGE DATA
# ═══════════════════════════════════════════════════════════════════════════


class TestMalformedData:
    """Verify the engine handles bad data gracefully."""

    def test_binary_garbage_file(self, engine: TabularEngine, tmp_path: Path) -> None:
        """Random binary data should fail to register, not crash."""
        garbage = tmp_path / "garbage.csv"
        garbage.write_bytes(os.urandom(1024))
        # DuckDB may or may not be able to read random bytes as CSV
        # Either it fails with an error or it reads something — both are acceptable
        import contextlib

        with contextlib.suppress(Exception):
            engine.register_file(garbage)

    def test_empty_file(self, engine: TabularEngine, tmp_path: Path) -> None:
        """Empty CSV file (0 bytes) — DuckDB creates a table with 0 columns."""
        empty = tmp_path / "empty.csv"
        empty.write_text("")
        # DuckDB reads empty file as empty table (no error)
        engine.register_file(empty)
        assert engine.count("empty") == 0

    def test_header_only_csv(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV with header but no data rows."""
        header_only = tmp_path / "header.csv"
        header_only.write_text("a,b,c\n")
        engine.register_file(header_only)
        assert engine.count("header") == 0

    def test_mismatched_column_counts(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV where rows have different numbers of fields.
        DuckDB strict mode may reject some rows."""
        ragged = tmp_path / "ragged.csv"
        ragged.write_text("a,b,c\n1,2,3\n4,5\n6,7,8,9\n")
        # DuckDB reads what it can — strict mode may reject mismatched rows
        engine.register_file(ragged)
        count = engine.count("ragged")
        assert count >= 1  # At least the valid row(s)

    def test_null_bytes_in_csv_data(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV with null bytes embedded in values."""
        null_csv = tmp_path / "nullbytes.csv"
        null_csv.write_text("x,y\nhello\x00world,test\n")
        # DuckDB should handle or reject gracefully
        import contextlib

        with contextlib.suppress(Exception):
            engine.register_file(null_csv)

    def test_extremely_long_single_line(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV with a single line that's 1MB+ (no newlines)."""
        long_csv = tmp_path / "longline.csv"
        long_csv.write_text("text\n" + "a" * 1_000_000 + "\n")
        engine.register_file(long_csv)
        result = engine.execute("SELECT LENGTH(text) FROM longline")
        assert result.rows[0][0] == 1_000_000

    def test_unicode_bom(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV with UTF-8 BOM (common from Excel exports)."""
        bom_csv = tmp_path / "bom.csv"
        bom_csv.write_text("\ufeffid,name\n1,Alice\n2,Bob\n")
        engine.register_file(bom_csv)
        count = engine.count("bom")
        assert count == 2

    def test_json_not_array(self, engine: TabularEngine, tmp_path: Path) -> None:
        """JSON file that's an object, not an array."""
        obj_json = tmp_path / "obj.json"
        obj_json.write_text('{"key": "value"}\n')
        # DuckDB's read_json_auto may or may not handle this
        import contextlib

        with contextlib.suppress(Exception):
            engine.register_file(obj_json)


# ═══════════════════════════════════════════════════════════════════════════
# 7. ALL-NULL AND DEGENERATE DATA
# ═══════════════════════════════════════════════════════════════════════════


class TestDegenerateData:
    """Pathological data patterns."""

    def test_all_null_values(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV where all data cells are empty."""
        null_csv = tmp_path / "allnull.csv"
        null_csv.write_text("a,b,c\n,,\n,,\n,,\n")
        engine.register_file(null_csv)
        result = engine.execute("SELECT COUNT(*), COUNT(a), COUNT(b) FROM allnull")
        assert result.rows[0][0] == 3  # 3 rows
        # COUNT(col) should be 0 for all-null columns
        assert result.rows[0][1] == 0

    def test_single_column_single_row(self, engine: TabularEngine, tmp_path: Path) -> None:
        """Smallest possible non-empty table."""
        tiny = tmp_path / "tiny.csv"
        tiny.write_text("x\n42\n")
        engine.register_file(tiny)
        result = engine.execute("SELECT * FROM tiny")
        assert len(result.rows) == 1
        assert len(result.columns) == 1

    def test_duplicate_column_names_csv(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV with duplicate header names — DuckDB auto-renames."""
        dup = tmp_path / "dup.csv"
        dup.write_text("x,x,x\n1,2,3\n")
        engine.register_file(dup)
        desc = engine.describe_table("dup")
        # DuckDB renames duplicates: x, x_1, x_2
        col_names = [c["name"] for c in desc["columns"]]
        assert len(col_names) == 3
        assert len(set(col_names)) == 3  # All unique after renaming

    def test_many_empty_rows(self, engine: TabularEngine, tmp_path: Path) -> None:
        """CSV with 1000 empty rows."""
        empty_rows = tmp_path / "emptyrows.csv"
        lines = ["a,b\n"] + [",\n"] * 1000
        empty_rows.write_text("".join(lines))
        engine.register_file(empty_rows)
        assert engine.count("emptyrows") == 1000

    def test_whitespace_only_values(self, engine: TabularEngine, tmp_path: Path) -> None:
        """Values that are just spaces/tabs."""
        ws = tmp_path / "whitespace.csv"
        ws.write_text("x,y\n   ,\t\n  ,  \n")
        engine.register_file(ws)
        result = engine.execute("SELECT * FROM whitespace")
        assert len(result.rows) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 8. (was PERFORMANCE BENCHMARKS — relocated to tests/benchmarks/test_engine_perf.py
#    per audit-01 KTAB-006 to keep wall-clock asserts out of the bounded
#    unit gate.)
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# 9. MULTI-ENGINE / ISOLATION
# ═══════════════════════════════════════════════════════════════════════════


class TestEngineIsolation:
    """Verify engines don't leak state between sessions."""

    def test_two_engines_isolated(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "iso.csv"
        _write_csv(csv_file, ["x"], [["1"]])

        engine1 = TabularEngine()
        engine2 = TabularEngine()

        engine1.register_file(csv_file, table_name="private")

        # engine2 should NOT see engine1's table
        assert engine2.list_tables() == []

        with pytest.raises(duckdb.CatalogException):
            engine2.execute("SELECT * FROM private")

        engine1.close()
        engine2.close()

    def test_file_backed_isolation(self, tmp_path: Path) -> None:
        """Two file-backed engines on different paths are isolated."""
        csv_file = tmp_path / "data.csv"
        _write_csv(csv_file, ["x"], [["1"]])

        db1 = tmp_path / "db1.duckdb"
        db2 = tmp_path / "db2.duckdb"

        engine1 = TabularEngine(db_path=db1)
        engine1.register_file(csv_file, table_name="t1")
        engine1.close()

        engine2 = TabularEngine(db_path=db2)
        assert engine2.list_tables() == []
        engine2.close()

    def test_sequential_engine_reuse(self, tmp_path: Path) -> None:
        """Create engine, close, create new one — no state leaks."""
        csv_file = tmp_path / "seq.csv"
        _write_csv(csv_file, ["x"], [["1"]])

        for i in range(10):
            with TabularEngine() as engine:
                engine.register_file(csv_file, table_name=f"t{i}")
                assert engine.count(f"t{i}") == 1


# ═══════════════════════════════════════════════════════════════════════════
# 10. CLI ADVERSARIAL INPUT
# ═══════════════════════════════════════════════════════════════════════════


class TestCLIAdversarial:
    """CLI with adversarial arguments."""

    def test_sql_with_special_chars(self, simple_csv: Path) -> None:
        """SQL with quotes, backticks, semicolons in the query string."""
        with mock.patch("sys.stdout", new_callable=StringIO), pytest.raises(SystemExit):
            cli_main(
                [
                    "query",
                    str(simple_csv),
                    "SELECT 1; DROP TABLE simple; --",
                ]
            )

    def test_nonexistent_format(self, simple_csv: Path, tmp_path: Path) -> None:
        """Export with unsupported format extension."""
        out = tmp_path / "out.xlsx"
        with pytest.raises(SystemExit):
            cli_main(["export", str(simple_csv), "--output", str(out)])

    def test_max_rows_zero(self, simple_csv: Path) -> None:
        """--max-rows 0 edge case."""
        import contextlib

        with mock.patch("sys.stdout", new_callable=StringIO), contextlib.suppress(SystemExit):
            cli_main(["query", str(simple_csv), "SELECT * FROM simple", "--max-rows", "0"])
