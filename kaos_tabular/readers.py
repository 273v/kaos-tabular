"""Convenience readers: CSV/Parquet/JSON file → TabularDocument.

Each function creates an ephemeral TabularEngine, registers the file
via DuckDB's native reader, converts to TabularDocument, and closes.
DuckDB handles all type inference, delimiter detection, and encoding.

Usage::

    from kaos_tabular import read_csv, read_json, read_parquet

    doc = read_csv("sales.csv")
    doc = read_json("records.json")
    doc = read_parquet("data.parquet")
"""

from __future__ import annotations

from pathlib import Path

from kaos_content.model.tabular import TabularDocument
from kaos_core.logging import get_logger

from kaos_tabular.engine import TabularEngine

logger = get_logger(__name__)


def read_csv(
    path: str | Path,
    *,
    table_name: str | None = None,
) -> TabularDocument:
    """Read a CSV/TSV file into a TabularDocument.

    DuckDB's ``read_csv_auto`` handles delimiter detection, header
    detection, type inference, and encoding automatically.

    Args:
        path: Path to the CSV/TSV file.
        table_name: Name for the table. Defaults to the file stem.

    Returns:
        TabularDocument with one table.
    """
    return _read_file(path, table_name=table_name)


def read_parquet(
    path: str | Path,
    *,
    table_name: str | None = None,
) -> TabularDocument:
    """Read a Parquet file into a TabularDocument.

    Args:
        path: Path to the Parquet file.
        table_name: Name for the table. Defaults to the file stem.

    Returns:
        TabularDocument with one table.
    """
    return _read_file(path, table_name=table_name)


def read_json(
    path: str | Path,
    *,
    table_name: str | None = None,
) -> TabularDocument:
    """Read a JSON/JSONL file into a TabularDocument.

    Supports both JSON arrays (``[{...}, ...]``) and newline-delimited
    JSON (one object per line).

    Args:
        path: Path to the JSON/JSONL file.
        table_name: Name for the table. Defaults to the file stem.

    Returns:
        TabularDocument with one table.
    """
    return _read_file(path, table_name=table_name)


def _read_file(
    path: str | Path,
    *,
    table_name: str | None = None,
) -> TabularDocument:
    """Shared implementation for all readers."""
    p = Path(path).resolve()
    if not p.is_file():
        msg = f"File not found: {p}"
        raise FileNotFoundError(msg)

    name = table_name or p.stem
    engine = TabularEngine()
    try:
        engine.register_file(p, table_name=name)
        return engine.to_tabular_document(name)
    finally:
        engine.close()
