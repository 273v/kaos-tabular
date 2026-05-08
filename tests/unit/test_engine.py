"""Tests for TabularEngine — session-scoped DuckDB wrapper."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from kaos_content.model.tabular import Column, ColumnType, Table, TabularDocument

from kaos_tabular.engine import TabularEngine


class TestEngineLifecycle:
    def test_init_in_memory(self) -> None:
        engine = TabularEngine()
        engine.close()

    def test_init_file_backed(self, tmp_path: Path) -> None:
        db = tmp_path / "test.duckdb"
        engine = TabularEngine(db_path=db)
        engine.close()
        assert db.exists()

    def test_context_manager(self) -> None:
        with TabularEngine() as engine:
            tables = engine.list_tables()
            assert tables == []

    def test_close_is_idempotent(self) -> None:
        engine = TabularEngine()
        engine.close()
        engine.close()  # Should not raise


class TestRegisterFile:
    def test_register_csv(self, engine: TabularEngine, simple_csv: Path) -> None:
        name = engine.register_file(simple_csv)
        assert name == "simple"
        tables = engine.list_tables()
        assert any(t["name"] == "simple" for t in tables)

    def test_register_csv_custom_name(self, engine: TabularEngine, simple_csv: Path) -> None:
        name = engine.register_file(simple_csv, table_name="my_data")
        assert name == "my_data"

    def test_register_json(self, engine: TabularEngine, records_json: Path) -> None:
        name = engine.register_file(records_json)
        assert name == "records"

    def test_register_missing_file(self, engine: TabularEngine) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            engine.register_file("/nonexistent/file.csv")

    def test_register_unsupported_format(self, engine: TabularEngine, tmp_path: Path) -> None:
        txt_file = tmp_path / "data.txt"
        txt_file.write_text("hello")
        with pytest.raises(ValueError, match="Unsupported file format"):
            engine.register_file(txt_file)

    def test_register_unicode(self, engine: TabularEngine, unicode_csv: Path) -> None:
        engine.register_file(unicode_csv)
        result = engine.execute("SELECT * FROM unicode")
        assert len(result.rows) == 5


class TestRegisterDocument:
    def test_register_document(self, engine: TabularEngine) -> None:
        doc = TabularDocument(
            tables=(
                Table(
                    name="t1",
                    columns=(Column("x", ColumnType.INTEGER),),
                    rows=((1,), (2,)),
                ),
                Table(
                    name="t2",
                    columns=(Column("y", ColumnType.TEXT),),
                    rows=(("a",), ("b",)),
                ),
            ),
        )
        names = engine.register_document(doc)
        assert set(names) == {"t1", "t2"}


class TestExecute:
    def test_simple_select(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        result = engine.execute("SELECT * FROM simple")
        assert len(result.rows) == 10
        assert len(result.columns) >= 5

    def test_select_with_where(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        result = engine.execute("SELECT * FROM simple WHERE id <= 3")
        assert len(result.rows) == 3

    def test_aggregate(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        result = engine.execute("SELECT COUNT(*) as cnt, SUM(amount) as total FROM simple")
        assert result.rows[0][0] == 10

    def test_max_rows_cap(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        result = engine.execute("SELECT * FROM simple", max_rows=3)
        assert len(result.rows) == 3

    def test_invalid_sql(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        with pytest.raises(duckdb.Error):
            engine.execute("INVALID SQL SYNTAX")

    def test_cross_table_join(
        self, engine: TabularEngine, simple_csv: Path, records_json: Path
    ) -> None:
        engine.register_file(simple_csv, table_name="csv_data")
        engine.register_file(records_json, table_name="json_data")
        result = engine.execute(
            "SELECT c.name, j.amount FROM csv_data c JOIN json_data j ON c.id = j.id ORDER BY c.id"
        )
        assert len(result.rows) == 5  # JSON has 5 rows


class TestIntrospection:
    def test_describe(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        desc = engine.describe_table("simple")
        assert desc["row_count"] == 10
        assert desc["column_count"] >= 5

    def test_list_tables_empty(self, engine: TabularEngine) -> None:
        assert engine.list_tables() == []

    def test_list_tables(self, engine: TabularEngine, simple_csv: Path, records_json: Path) -> None:
        engine.register_file(simple_csv)
        engine.register_file(records_json)
        tables = engine.list_tables()
        names = {t["name"] for t in tables}
        assert "simple" in names
        assert "records" in names

    def test_count(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        assert engine.count("simple") == 10

    def test_sample(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        result = engine.sample("simple", n=3)
        assert len(result.rows) == 3

    def test_to_tabular_document(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        doc = engine.to_tabular_document("simple")
        assert len(doc.tables) == 1
        assert doc.tables[0].name == "simple"
        assert doc.tables[0].row_count == 10

    def test_to_tabular_document_with_limit(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        doc = engine.to_tabular_document("simple", max_rows=3)
        assert len(doc.tables[0].rows) == 3
        assert doc.tables[0].row_count == 10  # Knows full count


class TestPersistence:
    def test_save_and_restore(
        self, engine: TabularEngine, simple_csv: Path, tmp_path: Path
    ) -> None:
        engine.register_file(simple_csv)
        export_dir = tmp_path / "export"
        engine.save(export_dir)
        engine.close()

        # Restore
        engine2 = TabularEngine()
        engine2._con.execute(f"IMPORT DATABASE '{export_dir}'")
        result = engine2.execute("SELECT COUNT(*) FROM simple")
        assert result.rows[0][0] == 10
        engine2.close()

    def test_file_backed_persistence(self, simple_csv: Path, tmp_path: Path) -> None:
        db = tmp_path / "persist.duckdb"
        engine1 = TabularEngine(db_path=db)
        engine1.register_file(simple_csv)
        engine1.close()

        engine2 = TabularEngine(db_path=db, read_only=True)
        assert engine2.count("simple") == 10
        engine2.close()


class TestHistory:
    def test_history_records_events(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        engine.execute("SELECT * FROM simple")
        history = engine.history()
        assert len(history) == 2
        assert history[0].event_type == "register"
        assert history[1].event_type == "query"

    def test_undo_last_register(self, engine: TabularEngine, simple_csv: Path) -> None:
        engine.register_file(simple_csv)
        assert engine.count("simple") == 10

        dropped = engine.undo_last_register()
        assert dropped == "simple"
        assert engine.list_tables() == []

    def test_undo_empty(self, engine: TabularEngine) -> None:
        assert engine.undo_last_register() is None
