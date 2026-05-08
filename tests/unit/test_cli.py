"""Tests for kaos-tabular CLI commands."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

from kaos_tabular.cli import main


class TestQueryCommand:
    def test_query_tsv(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["query", str(simple_csv), "SELECT * FROM simple WHERE id <= 2"])
        output = out.getvalue()
        assert "id" in output
        assert "Alice" in output

    def test_query_markdown(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["query", str(simple_csv), "SELECT * FROM simple LIMIT 2", "--format", "markdown"])
        output = out.getvalue()
        assert "|" in output  # markdown table uses pipes

    def test_query_max_rows(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["query", str(simple_csv), "SELECT * FROM simple", "--max-rows", "3"])
        output = out.getvalue()
        lines = [line for line in output.strip().split("\n") if line.strip()]
        assert len(lines) == 4  # header + 3 data rows

    def test_query_json_envelope(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["query", str(simple_csv), "SELECT COUNT(*) as cnt FROM simple", "--json"])
        data = json.loads(out.getvalue())
        assert data["command"] == "query"
        assert data["file"] == simple_csv.name
        assert data["row_count"] == 1


class TestDescribeCommand:
    def test_describe_human(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["describe", str(simple_csv)])
        output = out.getvalue()
        assert "Table:" in output
        assert "Rows:" in output

    def test_describe_json(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["describe", str(simple_csv), "--json"])
        data = json.loads(out.getvalue())
        assert data["command"] == "describe"
        assert data["row_count"] == 10


class TestSampleCommand:
    def test_sample_human(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["sample", str(simple_csv), "--rows", "3"])
        output = out.getvalue()
        assert "|" in output

    def test_sample_json(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["sample", str(simple_csv), "--json"])
        data = json.loads(out.getvalue())
        assert data["command"] == "sample"
        assert len(data["rows"]) == 5  # default 5


class TestExportCommand:
    def test_export_csv(self, simple_csv: Path, tmp_path: Path) -> None:
        out_file = tmp_path / "out.csv"
        main(["export", str(simple_csv), "--output", str(out_file)])
        assert out_file.exists()
        content = out_file.read_text()
        assert "Alice" in content

    def test_export_json(self, simple_csv: Path, tmp_path: Path) -> None:
        out_file = tmp_path / "out.json"
        main(["export", str(simple_csv), "--output", str(out_file)])
        assert out_file.exists()


class TestReadCommand:
    def test_read_human(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["read", str(simple_csv)])
        output = out.getvalue()
        assert "simple" in output
        assert "10" in output  # row count

    def test_read_json(self, simple_csv: Path) -> None:
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            main(["read", str(simple_csv), "--json"])
        data = json.loads(out.getvalue())
        assert data["command"] == "read"
        assert data["total_rows"] == 10
        assert len(data["tables"]) == 1


class TestErrorHandling:
    def test_missing_file(self) -> None:
        with pytest.raises(SystemExit):
            main(["describe", "/nonexistent/file.csv"])

    def test_no_command(self) -> None:
        with pytest.raises(SystemExit):
            main([])
