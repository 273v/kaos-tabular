"""Tests for convenience readers: read_csv, read_json, read_parquet."""

from __future__ import annotations

from pathlib import Path

import pytest

from kaos_tabular.readers import read_csv, read_json


class TestReadCSV:
    def test_basic(self, simple_csv: Path) -> None:
        doc = read_csv(simple_csv)
        assert len(doc.tables) == 1
        assert doc.tables[0].name == "simple"
        assert doc.tables[0].row_count == 10

    def test_custom_name(self, simple_csv: Path) -> None:
        doc = read_csv(simple_csv, table_name="my_data")
        assert doc.tables[0].name == "my_data"

    def test_columns_detected(self, simple_csv: Path) -> None:
        doc = read_csv(simple_csv)
        names = doc.tables[0].column_names()
        assert "id" in names
        assert "name" in names
        assert "amount" in names

    def test_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            read_csv("/nonexistent/file.csv")

    def test_unicode(self, unicode_csv: Path) -> None:
        doc = read_csv(unicode_csv)
        assert doc.tables[0].row_count == 5

    def test_values_accessible(self, simple_csv: Path) -> None:
        doc = read_csv(simple_csv)
        table = doc.tables[0]
        assert len(table.rows) == 10
        assert len(table.rows[0]) == len(table.columns)


class TestReadJSON:
    def test_basic(self, records_json: Path) -> None:
        doc = read_json(records_json)
        assert len(doc.tables) == 1
        assert doc.tables[0].row_count == 5

    def test_columns(self, records_json: Path) -> None:
        doc = read_json(records_json)
        names = doc.tables[0].column_names()
        assert "id" in names
        assert "name" in names

    def test_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            read_json("/nonexistent/file.json")

    def test_custom_name(self, records_json: Path) -> None:
        doc = read_json(records_json, table_name="my_json")
        assert doc.tables[0].name == "my_json"


class TestReadCSVEmpty:
    def test_empty_csv(self, tmp_path: Path) -> None:
        """CSV with header only, no data rows."""
        empty = tmp_path / "empty.csv"
        empty.write_text("a,b,c\n")
        doc = read_csv(empty)
        assert doc.tables[0].row_count == 0
        assert len(doc.tables[0].columns) == 3
