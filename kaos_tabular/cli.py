"""CLI for kaos-tabular: query, describe, sample, export, read.

Usage::

    kaos-tabular query data.csv "SELECT * FROM data WHERE amount > 100"
    kaos-tabular describe data.csv --json
    kaos-tabular sample data.csv --rows 5
    kaos-tabular export data.csv --output out.parquet
    kaos-tabular read data.csv --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from kaos_tabular.errors import EngineError


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``kaos-tabular`` CLI."""
    parser = argparse.ArgumentParser(
        prog="kaos-tabular",
        description="DuckDB-powered tabular data engine for KAOS",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- query --
    p_query = sub.add_parser("query", help="Execute SQL against a data file")
    p_query.add_argument("file", type=Path, help="Data file or .duckdb database")
    p_query.add_argument("sql", help="SQL query to execute")
    p_query.add_argument(
        "--format",
        choices=["tsv", "csv", "json", "markdown"],
        default="tsv",
        help="Output format (default: tsv)",
    )
    p_query.add_argument("--max-rows", type=int, default=1000, help="Max rows (default: 1000)")
    p_query.add_argument("--json", action="store_true", dest="json_out", help="JSON envelope")

    # -- describe --
    p_desc = sub.add_parser("describe", help="Describe table schema and stats")
    p_desc.add_argument("file", type=Path, help="Data file or .duckdb database")
    p_desc.add_argument("--table", help="Table name (for multi-table sources)")
    p_desc.add_argument("--json", action="store_true", dest="json_out", help="JSON envelope")

    # -- sample --
    p_sample = sub.add_parser("sample", help="Show sample rows from a table")
    p_sample.add_argument("file", type=Path, help="Data file or .duckdb database")
    p_sample.add_argument("--table", help="Table name (for multi-table sources)")
    p_sample.add_argument("--rows", type=int, default=5, help="Number of rows (default: 5)")
    p_sample.add_argument("--json", action="store_true", dest="json_out", help="JSON envelope")

    # -- export --
    p_export = sub.add_parser("export", help="Export table to file")
    p_export.add_argument("file", type=Path, help="Data file or .duckdb database")
    p_export.add_argument("--output", "-o", type=Path, required=True, help="Output file path")
    p_export.add_argument("--table", help="Table name (for multi-table sources)")
    p_export.add_argument(
        "--format",
        choices=["csv", "parquet", "json"],
        help="Output format (default: infer from extension)",
    )

    # -- read --
    p_read = sub.add_parser("read", help="Read file and show summary")
    p_read.add_argument("file", type=Path, help="Data file")
    p_read.add_argument("--json", action="store_true", dest="json_out", help="JSON envelope")

    args = parser.parse_args(argv)

    handlers: dict[str, Any] = {
        "query": _cmd_query,
        "describe": _cmd_describe,
        "sample": _cmd_sample,
        "export": _cmd_export,
        "read": _cmd_read,
    }

    try:
        handlers[args.command](args)
    except FileNotFoundError as exc:
        _error(str(exc))
    except Exception as exc:
        _error(str(exc))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _open_engine(path: Path) -> tuple[Any, str | None]:
    """Open an engine for the given path.

    Returns (engine, default_table_name). For .duckdb files, opens
    directly. For data files, creates in-memory engine and registers.
    """
    from kaos_tabular.engine import TabularEngine

    p = path.resolve()
    if not p.exists():
        msg = f"File not found: {p}"
        raise FileNotFoundError(msg)

    if p.suffix.lower() == ".duckdb":
        engine = TabularEngine(db_path=p, read_only=True)
        return engine, None

    engine = TabularEngine()
    table_name = engine.register_file(p)
    return engine, table_name


def _resolve_table(engine: Any, table_name: str | None) -> str:
    """Resolve the table name — use provided, or find the only table."""
    if table_name:
        return table_name
    tables = engine.list_tables()
    if len(tables) == 1:
        return tables[0]["name"]
    if not tables:
        _error("No tables found. Register a file first.")
    names = ", ".join(t["name"] for t in tables)
    _error(f"Multiple tables found: {names}. Use --table to specify one.")
    return ""  # unreachable


def _json_out(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _error(msg: str) -> NoReturn:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _cmd_query(args: argparse.Namespace) -> None:
    from kaos_content.serializers.tabular import (
        serialize_csv,
        serialize_json_records,
        serialize_markdown_table,
        serialize_tsv,
    )

    engine, _ = _open_engine(args.file)
    try:
        result = engine.execute(args.sql, max_rows=args.max_rows)

        if args.json_out:
            _json_out(
                {
                    "command": "query",
                    "file": args.file.name,
                    "sql": args.sql,
                    "row_count": len(result.rows),
                    "column_count": len(result.columns),
                    "columns": [c.name for c in result.columns],
                    "rows": [list(r) for r in result.rows],
                }
            )
            return

        formatters = {
            "tsv": serialize_tsv,
            "csv": serialize_csv,
            "json": serialize_json_records,
            "markdown": serialize_markdown_table,
        }
        print(formatters[args.format](result), end="")
    finally:
        engine.close()


def _cmd_describe(args: argparse.Namespace) -> None:
    engine, default_table = _open_engine(args.file)
    try:
        table_name = _resolve_table(engine, args.table or default_table)
        desc = engine.describe_table(table_name)

        if args.json_out:
            _json_out({"command": "describe", "file": args.file.name, **desc})
            return

        print(f"Table: {desc['name']}")
        print(f"Rows: {desc['row_count']}")
        print(f"Columns: {desc['column_count']}")
        print()
        for col in desc["columns"]:
            nullable = "nullable" if col.get("nullable", True) else "not null"
            print(f"  {col['name']:30s} {col['type']:15s} {nullable}")
    finally:
        engine.close()


def _cmd_sample(args: argparse.Namespace) -> None:
    from kaos_content.serializers.tabular import (
        serialize_markdown_table,
    )

    engine, default_table = _open_engine(args.file)
    try:
        table_name = _resolve_table(engine, args.table or default_table)
        result = engine.sample(table_name, n=args.rows)

        if args.json_out:
            _json_out(
                {
                    "command": "sample",
                    "file": args.file.name,
                    "table": table_name,
                    "row_count": len(result.rows),
                    "columns": [c.name for c in result.columns],
                    "rows": [list(r) for r in result.rows],
                }
            )
            return

        print(serialize_markdown_table(result, max_rows=0), end="")
    finally:
        engine.close()


def _cmd_export(args: argparse.Namespace) -> None:
    engine, default_table = _open_engine(args.file)
    try:
        table_name = _resolve_table(engine, args.table or default_table)

        # Determine format
        fmt = args.format
        if not fmt:
            ext = args.output.suffix.lower()
            fmt_map = {".csv": "csv", ".parquet": "parquet", ".pq": "parquet", ".json": "json"}
            fmt = fmt_map.get(ext)
            if not fmt:
                _error(
                    f"Cannot infer format from extension {ext!r}. Use --format csv|parquet|json."
                )

        try:
            row_count = engine.export_table(table_name, args.output, format=fmt)
        except EngineError as exc:
            _error(str(exc))
            return
        print(f"Exported {row_count} rows to {args.output}", file=sys.stderr)
    finally:
        engine.close()


def _cmd_read(args: argparse.Namespace) -> None:
    from kaos_content.serializers.tabular import serialize_tabular_summary

    from kaos_tabular.readers import _read_file

    doc = _read_file(args.file)

    if args.json_out:
        _json_out(
            {
                "command": "read",
                "file": args.file.name,
                "table_count": len(doc.tables),
                "total_rows": sum(t.row_count for t in doc.tables),
                "tables": [
                    {
                        "name": t.name,
                        "row_count": t.row_count,
                        "column_count": len(t.columns),
                        "columns": [
                            {"name": c.name, "type": c.column_type.value} for c in t.columns
                        ],
                    }
                    for t in doc.tables
                ],
            }
        )
        return

    print(serialize_tabular_summary(doc))
