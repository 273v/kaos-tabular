"""TabularEngine — session-scoped DuckDB wrapper for tabular data.

The engine owns a DuckDB connection and provides a high-level API for
registering data sources, executing SQL queries, and inspecting tables.
It delegates all DuckDB operations to ``kaos_content.bridges.duckdb``.

Usage::

    from kaos_tabular import TabularEngine

    with TabularEngine() as engine:
        engine.register_file("sales.csv")
        result = engine.execute("SELECT region, SUM(amount) FROM sales GROUP BY region")
        print(result.rows)

For session persistence::

    engine = TabularEngine(db_path="session.duckdb")
    engine.register_file("data.csv")
    engine.close()  # data persists

    engine2 = TabularEngine(db_path="session.duckdb", read_only=True)
    result = engine2.execute("SELECT * FROM data")
"""

from __future__ import annotations

import datetime
import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import duckdb
from kaos_content.bridges.duckdb import (
    _quote_ident,
)
from kaos_content.bridges.duckdb import (
    describe_table as _bridge_describe,
)
from kaos_content.bridges.duckdb import (
    list_tables as _bridge_list,
)
from kaos_content.bridges.duckdb import (
    query_to_table as _bridge_query,
)
from kaos_content.bridges.duckdb import (
    register_document as _bridge_register_doc,
)
from kaos_content.bridges.duckdb import (
    register_table as _bridge_register_table,
)
from kaos_content.model.metadata import DocumentMetadata
from kaos_content.model.tabular import (
    Table,
    TabularDocument,
)
from kaos_core.logging import get_logger

from kaos_tabular.errors import EngineError, RegistrationError

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# SQL escaping
# ---------------------------------------------------------------------------


def _q_lit(s: str) -> str:
    """Quote ``s`` as a DuckDB SQL string literal.

    Doubles every single quote and wraps the result in single quotes,
    so the output can be interpolated into SQL without risk of
    literal-escape injection. Used for COPY/EXPORT target paths and
    for the ``src_table`` names returned by ``sqlite_master``, both
    of which can carry attacker-controlled content (a hostile
    .sqlite file controls its own table names; an MCP-supplied
    output_path is caller-supplied).
    """
    return "'" + s.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Did-you-mean suggestions
# ---------------------------------------------------------------------------
#
# Every error path that mentions a missing table or column tries to
# suggest what the agent likely meant, using ``difflib.get_close_matches``
# with a 0.6 ratio cutoff. The cutoff is high enough to avoid spurious
# matches on short identifiers (``id`` vs ``ip`` would both match below
# 0.5) but low enough to forgive single-character typos on typical
# 6+ character column names.


def _suggestions(name: str, universe: Sequence[str], *, n: int = 3) -> list[str]:
    """Return up to ``n`` strings from ``universe`` similar to ``name``.

    Wrapper around :func:`difflib.get_close_matches` returning ``[]``
    on an empty universe so callers can short-circuit without checking
    bounds.
    """
    if not universe:
        return []
    return difflib.get_close_matches(name, universe, n=n, cutoff=0.6)


def _did_you_mean_fragment(matches: Sequence[str]) -> str:
    """Format a list of suggestions as a "Did you mean ...?" sentence.

    Returns an empty string for an empty list so callers can splice
    the fragment into their error messages unconditionally.
    """
    if not matches:
        return ""
    if len(matches) == 1:
        return f"Did you mean {matches[0]!r}?"
    quoted = ", ".join(repr(m) for m in matches)
    return f"Did you mean one of: {quoted}?"


# ---------------------------------------------------------------------------
# Event tracking
# ---------------------------------------------------------------------------

_MAX_ROWS_HARD_CAP = 10_000

# Aggregate functions accepted by ``TabularEngine.aggregate``. Order is
# the documented ordering (most common first) — agents reading the
# whitelist via error messages benefit from a predictable layout.
_AGGREGATE_FUNCTIONS: tuple[str, ...] = (
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "count_distinct",
    "median",
    "stddev",
    "variance",
    "first",
    "last",
)
_AGGREGATE_FUNCTIONS_SET: frozenset[str] = frozenset(_AGGREGATE_FUNCTIONS)
_ORDER_DIRECTIONS: frozenset[str] = frozenset({"asc", "desc"})


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """A recorded engine operation for history/audit."""

    timestamp: datetime.datetime
    event_type: Literal["register", "query", "drop"]
    detail: str
    table_names: tuple[str, ...]


# ---------------------------------------------------------------------------
# File format detection
# ---------------------------------------------------------------------------

_FORMAT_READERS: dict[str, str] = {
    ".csv": "read_csv_auto",
    ".tsv": "read_csv_auto",
    ".parquet": "read_parquet",
    ".pq": "read_parquet",
    ".json": "read_json_auto",
    ".jsonl": "read_json_auto",
    ".ndjson": "read_json_auto",
}

# Formats that need special handling (not a simple DuckDB reader function)
_SPECIAL_FORMATS = frozenset({".sqlite", ".db", ".sqlite3"})


def _duckdb_reader_for_path(path: Path) -> str | None:
    """Return the DuckDB reader function name for a file extension.

    Returns None for formats that need special handling (SQLite).

    XLSX / XLSM / XLS are intentionally NOT handled here — see
    ``register_file`` for the migration note. Parse XLSX with
    ``kaos_office.parse_xlsx(path)`` and pass each ``Table`` to
    ``engine.register_table(table, name=...)``.
    """
    ext = path.suffix.lower()
    reader = _FORMAT_READERS.get(ext)
    if reader is not None:
        return reader
    if ext in _SPECIAL_FORMATS:
        return None  # Handled specially in register_file
    supported = ", ".join(sorted([*_FORMAT_READERS.keys(), *_SPECIAL_FORMATS]))
    msg = (
        f"Unsupported file format: {ext!r}. "
        f"Supported: {supported}. "
        f"For .duckdb files, open directly with TabularEngine(db_path=...). "
        f"For .xlsx / .xlsm / .xls files, parse with kaos_office.parse_xlsx() "
        f"and pass each Table to engine.register_table()."
    )
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# TabularEngine
# ---------------------------------------------------------------------------


class TabularEngine:
    """Session-scoped DuckDB engine for tabular data.

    Wraps a DuckDB connection with registration, query, and introspection
    methods. All DuckDB operations delegate to ``kaos_content.bridges.duckdb``.

    Args:
        db_path: Path to a .duckdb file for persistent storage.
            ``None`` (default) creates an in-memory database.
        read_only: Open the database in read-only mode. Multiple
            concurrent readers can share a file-backed database.
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        read_only: bool = False,
    ) -> None:
        db_str = str(db_path) if db_path else ""
        self._con: duckdb.DuckDBPyConnection = duckdb.connect(db_str, read_only=read_only)
        self._db_path = Path(db_path) if db_path else None
        self._read_only = read_only
        self._history: list[EngineEvent] = []
        self._registered: list[str] = []

    # -- Context manager ---------------------------------------------------

    def __enter__(self) -> TabularEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- Registration ------------------------------------------------------

    def register_file(
        self,
        path: str | Path,
        *,
        table_name: str | None = None,
    ) -> str:
        """Register a CSV/Parquet/JSON file as a queryable table.

        DuckDB reads the file natively — type inference, delimiter
        detection, and encoding detection are all handled by DuckDB.

        Args:
            path: Path to the data file.
            table_name: Name for the table. Defaults to the file stem.

        Returns:
            The registered table name.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file format is not supported.
        """
        p = Path(path).resolve()
        if not p.is_file():
            msg = f"File not found: {p}"
            raise FileNotFoundError(msg)

        reader = _duckdb_reader_for_path(p)
        name = table_name or p.stem
        ext = p.suffix.lower()

        if reader is not None:
            # Standard DuckDB reader (CSV, JSON, Parquet)
            escaped_path = str(p).replace("'", "''")
            sql = (
                f"CREATE OR REPLACE TABLE {_quote_ident(name)} AS "
                f"SELECT * FROM {reader}('{escaped_path}')"
            )
            self._con.execute(sql)
            registered = (name,)
        elif ext in (".sqlite", ".db", ".sqlite3"):
            # SQLite → DuckDB sqlite scanner. May register 1..N tables.
            # The returned tuple is the actual set of DuckDB tables
            # created, NOT the placeholder ``name`` — for a multi-table
            # SQLite source the placeholder is never a real table.
            # audit-04/kaos-tabular.md F-001.
            registered = self._register_sqlite(p, name)
        else:
            msg = f"No handler for format: {ext!r}"
            raise ValueError(msg)

        # Record every DuckDB table created, not just the placeholder.
        # Mirrors what ``list_tables()`` will see + lets ``undo_last_register``
        # actually drop the tables it claimed it created.
        for created in registered:
            if created not in self._registered:
                self._registered.append(created)
        self._record("register", f"file:{p.name}", tuple(registered))
        # Return contract: single string for back-compat. For multi-table
        # SQLite the caller gets the first table name; call ``list_tables()``
        # or read the most recent ``history()`` entry to see all of them.
        return registered[0]

    def _register_sqlite(self, path: Path, name: str) -> tuple[str, ...]:
        """Register SQLite tables via DuckDB sqlite scanner.

        Returns the tuple of DuckDB table names actually created. For a
        single-table SQLite source this is ``(name,)``. For multi-table
        sources each SQLite source table becomes its own DuckDB table —
        either ``(src_table_1, src_table_2, …)`` when the caller did
        not pass an explicit ``table_name`` (the inferred ``name`` is
        the file stem and is NOT a real table) or
        ``(name_src_table_1, name_src_table_2, …)`` when an explicit
        ``table_name`` was passed.

        audit-04/kaos-tabular.md F-001: the previous version returned
        ``None`` and the caller unconditionally appended ``name`` to
        ``self._registered`` and recorded ``(name,)`` in history — but
        for the multi-table branch no DuckDB table called ``name``
        exists, so ``undo_last_register`` and ``list_tables`` diverged.
        """
        try:
            self._con.execute("INSTALL sqlite")
            self._con.execute("LOAD sqlite")
        except duckdb.Error as exc:
            msg = (
                "DuckDB sqlite extension is required to read SQLite files. "
                "Install with `duckdb extension install sqlite` (online), "
                "pre-bundle the extension in your container image for offline "
                "deployments, or alternatively pre-export the SQLite tables "
                "to CSV / Parquet and register those files instead."
            )
            raise RegistrationError(msg) from exc
        # ``_q_lit`` produces a SQL string-literal-safe version of an
        # arbitrary Python string. Used for both the path and the
        # ``src_table`` names returned by sqlite_master (which is data
        # inside the .sqlite file — an attacker who controls the file
        # controls those names).
        escaped = _q_lit(str(path))

        # List tables in the SQLite database. ``escaped`` is the source
        # SQLite file path, already quoted via ``_q_lit`` above; the
        # second argument is the literal string ``'sqlite_master'``. No
        # attacker-controlled fragment is interpolated raw.
        tables_result = self._con.execute(
            f"SELECT name FROM sqlite_scan({escaped}, 'sqlite_master') "  # nosec B608
            f"WHERE type='table' ORDER BY name"
        ).fetchall()

        if not tables_result:
            msg = f"No tables found in SQLite database: {path.name}"
            raise ValueError(msg)

        if len(tables_result) == 1:
            # Single table: use requested name. All three interpolations
            # below are passed through validating quoters:
            # ``_quote_ident`` (identifier — validates + double-quotes),
            # ``_q_lit`` (string literal — doubles single quotes).
            src_table = tables_result[0][0]
            self._con.execute(
                f"CREATE OR REPLACE TABLE {_quote_ident(name)} AS "  # nosec B608
                f"SELECT * FROM sqlite_scan({escaped}, {_q_lit(src_table)})"
            )
            return (name,)
        # Multiple tables: register each. Same quoting contract as the
        # single-table branch above. We do NOT call ``self._registered.append``
        # here — the caller (``register_file``) owns that bookkeeping and
        # uses the returned tuple as its source of truth (audit-04 F-001).
        created: list[str] = []
        for (src_table,) in tables_result:
            tgt = f"{name}_{src_table}" if name != path.stem else src_table
            self._con.execute(
                f"CREATE OR REPLACE TABLE {_quote_ident(tgt)} AS "  # nosec B608
                f"SELECT * FROM sqlite_scan({escaped}, {_q_lit(src_table)})"
            )
            created.append(tgt)
        return tuple(created)

    def register_table(self, table: Table, *, name: str | None = None) -> str:
        """Register a kaos-content Table as a queryable DuckDB table.

        Args:
            table: The Table to register.
            name: View name. Defaults to ``table.name``.

        Returns:
            The registered table name.
        """
        view_name = _bridge_register_table(self._con, table, name=name)
        self._registered.append(view_name)
        self._record("register", f"table:{view_name}", (view_name,))
        return view_name

    def register_document(
        self,
        doc: TabularDocument,
        *,
        prefix: str = "",
    ) -> list[str]:
        """Register all tables from a TabularDocument.

        Args:
            doc: The document whose tables to register.
            prefix: Optional prefix for table names.

        Returns:
            List of registered table names.
        """
        names = _bridge_register_doc(self._con, doc, prefix=prefix)
        self._registered.extend(names)
        self._record("register", f"document:{len(names)} tables", tuple(names))
        return names

    # -- Query -------------------------------------------------------------

    def execute(self, sql: str, *, max_rows: int = 1000) -> Table:
        """Execute a SQL query and return results as a Table.

        Args:
            sql: SQL query string.
            max_rows: Maximum rows to return. Default 1000, hard cap 10,000.

        Returns:
            Table with typed columns and row data.
        """
        capped = min(max_rows, _MAX_ROWS_HARD_CAP)
        limited_sql = f"SELECT * FROM ({sql}) AS _q LIMIT {capped}"
        result = _bridge_query(self._con, limited_sql, name="result")
        self._record("query", sql[:200], ())
        return result

    # -- Introspection -----------------------------------------------------

    def describe_table(self, table_name: str) -> dict[str, Any]:
        """Describe a table: columns, types, row count, sample values.

        Returns a dict suitable for MCP tool responses and agent consumption.
        """
        self._assert_table_exists(table_name, op="describe_table")
        return _bridge_describe(self._con, table_name)

    def list_tables(self) -> list[dict[str, Any]]:
        """List all registered tables with dimensions."""
        return _bridge_list(self._con)

    def count(self, table_name: str) -> int:
        """Return the row count for a table."""
        self._assert_table_exists(table_name, op="count")
        # ``_quote_ident`` validates + double-quotes the identifier.
        # ``_assert_table_exists`` rejects any name not currently
        # registered, so this is also bounded by the catalog.
        result = self._con.execute(
            f"SELECT COUNT(*) FROM {_quote_ident(table_name)}"  # nosec B608
        ).fetchone()
        return result[0] if result else 0

    def sample(self, table_name: str, n: int = 5) -> Table:
        """Return a random sample of N rows from a table."""
        self._assert_table_exists(table_name, op="sample")
        sql = f"SELECT * FROM {_quote_ident(table_name)} USING SAMPLE {n} ROWS"
        return _bridge_query(self._con, sql, name=table_name)

    # -- Analytical helpers ------------------------------------------------

    def _column_names(self, table_name: str) -> list[str]:
        """Return the registered columns of ``table_name`` (DuckDB catalog)."""
        # ``_q_lit`` quotes the value as a SQL string literal (doubles
        # single quotes). The query reads ``information_schema.columns``
        # which is itself read-only — even a hostile ``table_name``
        # cannot mutate state through this query.
        rows = self._con.execute(
            "SELECT column_name FROM information_schema.columns "  # nosec B608
            f"WHERE table_name = {_q_lit(table_name)} "
            "ORDER BY ordinal_position"
        ).fetchall()
        return [r[0] for r in rows]

    def _numeric_column_names(self, table_name: str) -> list[str]:
        """Return columns of ``table_name`` whose DuckDB type is numeric."""
        # Same safety contract as ``_column_names`` above.
        rows = self._con.execute(
            "SELECT column_name FROM information_schema.columns "  # nosec B608
            f"WHERE table_name = {_q_lit(table_name)} "
            "  AND data_type IN ('TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', "
            "                    'HUGEINT', 'UTINYINT', 'USMALLINT', 'UINTEGER', "
            "                    'UBIGINT', 'UHUGEINT', 'FLOAT', 'DOUBLE', 'DECIMAL') "
            "ORDER BY ordinal_position"
        ).fetchall()
        return [r[0] for r in rows]

    # -- Validation helpers (table / column existence + did-you-mean) -----

    def _registered_table_names(self) -> list[str]:
        """All tables visible in the current DuckDB catalog (main schema)."""
        rows = self._con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        return [r[0] for r in rows]

    def _table_exists(self, name: str) -> bool:
        # ``_q_lit`` quotes the value as a SQL string literal; read-only
        # query against ``information_schema.tables``.
        rows = self._con.execute(
            "SELECT 1 FROM information_schema.tables "  # nosec B608
            f"WHERE table_schema = 'main' AND table_name = {_q_lit(name)} LIMIT 1"
        ).fetchall()
        return bool(rows)

    def _assert_table_exists(self, name: str, *, op: str) -> None:
        """Raise :class:`EngineError` with a did-you-mean suggestion if missing."""
        if self._table_exists(name):
            return
        universe = self._registered_table_names()
        suggestion = _did_you_mean_fragment(_suggestions(name, universe))
        available = ", ".join(universe) if universe else "(none registered)"
        bits = [f"{op}: table {name!r} not found."]
        if suggestion:
            bits.append(suggestion)
        bits.extend(
            [
                f"Available tables: {available}.",
                "How to fix: register the source first with register_file() / register_table().",
                "Alternative: call list_tables() to see what's registered.",
            ]
        )
        raise EngineError(" ".join(bits))

    def _assert_columns_exist(
        self,
        table_name: str,
        columns: Iterable[str],
        *,
        op: str,
    ) -> None:
        """Raise :class:`EngineError` listing missing columns + suggestions.

        Caller is responsible for asserting the table itself exists
        first; the column universe is empty for an unknown table and
        the error message degrades gracefully.
        """
        universe = self._column_names(table_name)
        cols = list(columns)
        missing = [c for c in cols if c not in universe]
        if not missing:
            return
        miss_pieces: list[str] = []
        for m in missing:
            sug = _did_you_mean_fragment(_suggestions(m, universe))
            miss_pieces.append(f"{m!r}" + (f" ({sug})" if sug else ""))
        available = ", ".join(universe) if universe else "(no columns)"
        msg = (
            f"{op}: column(s) not found in {table_name!r}: "
            f"{', '.join(miss_pieces)}. "
            f"Available columns: {available}. "
            f"How to fix: pick one of the available columns. "
            f"Alternative: call describe_table({table_name!r}) for the full schema."
        )
        raise EngineError(msg)

    def find_duplicates(
        self,
        table_name: str,
        *,
        columns: list[str] | None = None,
    ) -> Table:
        """Return rows whose values in ``columns`` appear in more than one row.

        With ``columns=None``, every column is used (full-row duplicate
        detection). The returned ``Table`` has the same shape as the input
        table; rows that appear only once are omitted.

        Implementation uses DuckDB's ``QUALIFY`` clause so window-filter
        semantics are explicit and the SQL is one statement.
        """
        self._assert_table_exists(table_name, op="find_duplicates")
        if columns is None:
            columns = self._column_names(table_name)
        if not columns:
            msg = (
                f"find_duplicates({table_name!r}): no columns to group by. "
                "How to fix: pass columns=['...'] explicitly. "
                "Alternative: register a non-empty table first."
            )
            raise EngineError(msg)
        self._assert_columns_exist(table_name, columns, op="find_duplicates")
        quoted_cols = ", ".join(_quote_ident(c) for c in columns)
        quoted_table = _quote_ident(table_name)
        sql = f"SELECT * FROM {quoted_table} QUALIFY COUNT(*) OVER (PARTITION BY {quoted_cols}) > 1"
        try:
            result = _bridge_query(self._con, sql, name=f"{table_name}_duplicates")
        except duckdb.Error as exc:
            msg = (
                f"find_duplicates failed for {table_name!r}: {exc}. "
                f"How to fix: verify every column in {columns!r} exists in the "
                "table. Alternative: call describe_table() to inspect the schema."
            )
            raise EngineError(msg) from exc
        self._record("query", f"find_duplicates:{table_name}", (table_name,))
        return result

    def correlation(
        self,
        table_name: str,
        *,
        columns: list[str] | None = None,
    ) -> Table:
        """Pairwise Pearson correlation between numeric columns.

        Returns a long-form ``Table`` with columns ``(col_a, col_b, corr)``.
        With ``columns=None``, every numeric column in the table is included.
        Self-pairs (``col_a == col_b``) are emitted (correlation 1.0).
        """
        self._assert_table_exists(table_name, op="correlation")
        if columns is None:
            columns = self._numeric_column_names(table_name)
        if len(columns) < 2:
            msg = (
                f"correlation({table_name!r}): need at least 2 numeric columns, "
                f"got {len(columns)}. How to fix: pass columns=[...] explicitly "
                "with two or more numeric column names. Alternative: call "
                "describe_table() to see which columns are numeric."
            )
            raise EngineError(msg)
        self._assert_columns_exist(table_name, columns, op="correlation")
        quoted_table = _quote_ident(table_name)
        # Build N x N UNION ALL of single-row CORR(col_i, col_j) selects.
        unions = [
            (
                f"SELECT {_q_lit(a)} AS col_a, {_q_lit(b)} AS col_b, "
                f"CORR({_quote_ident(a)}, {_quote_ident(b)}) AS corr "
                f"FROM {quoted_table}"
            )
            for a in columns
            for b in columns
        ]
        sql = " UNION ALL ".join(unions)
        try:
            result = _bridge_query(self._con, sql, name=f"{table_name}_correlation")
        except duckdb.Error as exc:
            msg = (
                f"correlation failed for {table_name!r}: {exc}. "
                f"How to fix: verify every column in {columns!r} exists and is "
                "numeric. Alternative: omit columns= to auto-select numeric ones."
            )
            raise EngineError(msg) from exc
        self._record("query", f"correlation:{table_name}", (table_name,))
        return result

    # -- Structured shape operations (aggregate / filter / top_k) ---------

    @staticmethod
    def _agg_expression(func: str, column: str, alias: str | None) -> str:
        """Render one ``Aggregation`` tuple as a DuckDB SELECT-list expression.

        Encapsulates the special cases:

        * ``count`` with column ``*`` → ``COUNT(*)``;
        * ``count_distinct`` → ``COUNT(DISTINCT col)``;
        * everything else → ``FUNC(col)``.

        The caller is expected to have already validated ``func`` against
        :data:`_AGGREGATE_FUNCTIONS_SET` and ``column`` against the
        table's actual schema (or to have permitted ``*`` for
        ``count``).
        """
        norm = func.lower().strip()
        if norm == "count" and column == "*":
            body = "COUNT(*)"
        elif norm == "count_distinct":
            body = f"COUNT(DISTINCT {_quote_ident(column)})"
        else:
            body = f"{norm.upper()}({_quote_ident(column)})"
        if alias:
            body += f" AS {_quote_ident(alias)}"
        return body

    def _validate_aggregates(
        self,
        table_name: str,
        aggregates: Sequence[tuple[str, str, str | None]],
    ) -> None:
        """Validate every ``(func, column, alias)`` triple before SQL gen.

        Bad function names get a "Did you mean ...?" against the
        whitelist; bad columns get one against the table's schema.
        ``column == '*'`` is only legal for ``count``; ``count_distinct``
        requires a real column.
        """
        if not aggregates:
            msg = (
                "aggregate(...): aggregates= must list at least one "
                "(func, column[, alias]) entry. "
                f"How to fix: pass aggregates=[('sum', 'amount')] or one of "
                f"the supported funcs: {', '.join(_AGGREGATE_FUNCTIONS)}. "
                "Alternative: for distinct-row queries with no aggregates, "
                "call kaos-tabular-query with `SELECT DISTINCT ...`."
            )
            raise EngineError(msg)

        # Function name validation pass.
        bad_funcs: list[tuple[str, str]] = []
        for func, _, _ in aggregates:
            if func.lower().strip() not in _AGGREGATE_FUNCTIONS_SET:
                sug = _did_you_mean_fragment(_suggestions(func, _AGGREGATE_FUNCTIONS))
                bad_funcs.append((func, sug))
        if bad_funcs:
            pieces = [f"{f!r}" + (f" ({s})" if s else "") for f, s in bad_funcs]
            msg = (
                f"aggregate(...): unsupported function(s): {', '.join(pieces)}. "
                f"Supported: {', '.join(_AGGREGATE_FUNCTIONS)}. "
                "How to fix: pick one of the supported names. "
                "Alternative: use kaos-tabular-query with the raw SQL aggregate."
            )
            raise EngineError(msg)

        # Column validation pass — ``*`` is only legal with ``count``.
        cols_needed: list[str] = []
        for func, col, _alias in aggregates:
            if col == "*":
                if func.lower().strip() != "count":
                    msg = (
                        f"aggregate(...): column='*' is only valid with func='count'; "
                        f"got func={func!r}. "
                        "How to fix: pass column='<name>' or change func to 'count'. "
                        "Alternative: see describe_table() for available columns."
                    )
                    raise EngineError(msg)
                continue
            cols_needed.append(col)
        if cols_needed:
            self._assert_columns_exist(table_name, cols_needed, op="aggregate(aggregates=)")

    def aggregate(
        self,
        table_name: str,
        *,
        aggregates: Sequence[tuple[str, str] | tuple[str, str, str | None]],
        group_by: Sequence[str] | None = None,
        where: str | None = None,
        having: str | None = None,
        order_by: Sequence[tuple[str, str]] | None = None,
        limit: int | None = None,
        target: str | None = None,
    ) -> Table:
        """Composed GROUP BY operation with typed validation.

        ``aggregates`` is a list of ``(func, column)`` or
        ``(func, column, alias)`` tuples. ``func`` is one of
        :data:`_AGGREGATE_FUNCTIONS`; ``column`` must exist on
        ``table_name`` (with the exception of ``*`` for ``func='count'``).

        ``group_by`` is optional — without it the operation collapses to
        a single-row aggregation. Bare ``GROUP BY`` with no aggregates
        is rejected; that is the ``SELECT DISTINCT`` shape and the user
        should reach for the SQL ``query`` tool instead.

        ``where`` and ``having`` are opaque DuckDB SQL fragments
        (filtering predicates have effectively unbounded shape; trying
        to type them would be a fool's errand). They are interpolated
        directly into the generated SQL — the engine's trust contract
        is that the caller already had SQL execution authority.

        ``order_by`` items must reference either a ``group_by`` column
        or an explicit aggregate alias.

        If ``target`` is set the result is materialized as
        ``CREATE OR REPLACE TABLE <target>`` and registered with the
        engine; otherwise it is computed but not persisted.
        """
        self._assert_table_exists(table_name, op="aggregate")

        # Normalize aggregates to (func, column, alias?) triples.
        # ty can't narrow tuple length from runtime ``len()`` checks,
        # so cast the 3-tuple branch explicitly. The runtime ``len()``
        # guard is still load-bearing for the callers that pass the
        # union shape (the MCP coercion produces 3-tuples directly,
        # so the ``len() == 2`` branch only fires from Python callers).
        normalized: list[tuple[str, str, str | None]] = []
        for entry in aggregates:
            entry_len = len(entry)
            if entry_len == 2:
                two = cast(tuple[str, str], entry)
                normalized.append((two[0], two[1], None))
            elif entry_len == 3:
                three = cast(tuple[str, str, str | None], entry)
                normalized.append(three)
            else:  # pragma: no cover — guarded by typing in callers
                msg = (
                    "aggregate(...): each aggregates= entry must be "
                    "(func, column) or (func, column, alias). "
                    f"Got: {entry!r}."
                )
                raise EngineError(msg)
        self._validate_aggregates(table_name, normalized)

        # group_by validation.
        gb_list: list[str] = list(group_by) if group_by else []
        if gb_list:
            self._assert_columns_exist(table_name, gb_list, op="aggregate(group_by=)")

        # order_by validation: every reference must be either a
        # group_by column or an explicit aggregate alias.
        ob_list: list[tuple[str, str]] = list(order_by) if order_by else []
        if ob_list:
            allowed = set(gb_list)
            for _f, _c, alias in normalized:
                if alias:
                    allowed.add(alias)
            for col, direction in ob_list:
                if direction.lower() not in _ORDER_DIRECTIONS:
                    msg = (
                        f"aggregate(order_by=): direction must be 'asc' or 'desc', "
                        f"got {direction!r}. "
                        "How to fix: pass [('region', 'asc')] or "
                        "[('total', 'desc')]."
                    )
                    raise EngineError(msg)
                if col not in allowed:
                    sug = _did_you_mean_fragment(_suggestions(col, sorted(allowed)))
                    msg = (
                        f"aggregate(order_by=): {col!r} is not a group_by "
                        f"column or aggregate alias. "
                        + (f"{sug} " if sug else "")
                        + (
                            f"Available: {', '.join(sorted(allowed))}. "
                            if allowed
                            else "(no group_by columns or aliases). "
                        )
                        + "How to fix: add an alias to the aggregate "
                        "(e.g. ('sum', 'units', 'total_units')) and order by it."
                    )
                    raise EngineError(msg)

        # SQL generation.
        select_pieces: list[str] = [_quote_ident(c) for c in gb_list]
        select_pieces.extend(self._agg_expression(*t) for t in normalized)
        select_csv = ", ".join(select_pieces)

        sql_parts = [f"SELECT {select_csv} FROM {_quote_ident(table_name)}"]
        if where:
            sql_parts.append(f"WHERE {where}")
        if gb_list:
            gb_csv = ", ".join(_quote_ident(c) for c in gb_list)
            sql_parts.append(f"GROUP BY {gb_csv}")
        if having:
            if not gb_list:
                msg = (
                    "aggregate(having=): HAVING requires group_by=. "
                    "How to fix: pass group_by=['<col>'] or fold the predicate "
                    "into where=. Alternative: use kaos-tabular-query for "
                    "ungrouped HAVING."
                )
                raise EngineError(msg)
            sql_parts.append(f"HAVING {having}")
        if ob_list:
            ob_csv = ", ".join(f"{_quote_ident(c)} {direction.upper()}" for c, direction in ob_list)
            sql_parts.append(f"ORDER BY {ob_csv}")
        if limit is not None:
            if limit < 1:
                msg = (
                    f"aggregate(limit={limit}): must be >= 1. "
                    "How to fix: omit limit= for unbounded results."
                )
                raise EngineError(msg)
            sql_parts.append(f"LIMIT {int(limit)}")
        sql = " ".join(sql_parts)

        if target is not None:
            ddl = f"CREATE OR REPLACE TABLE {_quote_ident(target)} AS {sql}"
            try:
                self._con.execute(ddl)
            except duckdb.Error as exc:
                msg = (
                    f"aggregate failed: {exc}. "
                    "How to fix: verify the where / having clauses are valid "
                    "DuckDB SQL fragments and reference real columns. "
                    "Alternative: drop where / having and rerun, or use "
                    "kaos-tabular-query for raw SQL."
                )
                raise EngineError(msg) from exc
            if target not in self._registered:
                self._registered.append(target)
            self._record("query", f"aggregate:{table_name}->{target}", (table_name, target))
            return _bridge_query(
                self._con,
                f"SELECT * FROM {_quote_ident(target)}",
                name=target,
            )

        try:
            result = _bridge_query(self._con, sql, name=f"{table_name}_aggregate")
        except duckdb.Error as exc:
            msg = (
                f"aggregate failed: {exc}. "
                "How to fix: verify the where / having clauses are valid "
                "DuckDB SQL fragments and reference real columns. "
                "Alternative: drop where / having and rerun, or use "
                "kaos-tabular-query for raw SQL."
            )
            raise EngineError(msg) from exc
        self._record("query", f"aggregate:{table_name}", (table_name,))
        return result

    def filter(
        self,
        table_name: str,
        *,
        where: str,
        limit: int | None = None,
        target: str | None = None,
    ) -> Table:
        """Apply a structured ``WHERE`` filter and return the matching rows.

        ``where`` is an opaque DuckDB SQL fragment (predicate shapes are
        unbounded; see ``aggregate`` for the same reasoning). The
        engine validates the table exists and that ``where`` is non-empty
        — DuckDB validates the predicate.

        Materializes to ``target`` when set; otherwise returns the
        result without persisting.
        """
        self._assert_table_exists(table_name, op="filter")
        if not where or not where.strip():
            msg = (
                "filter(where=...): where= must be a non-empty SQL predicate. "
                "How to fix: pass where='units > 100' or "
                "where=\"region = 'east'\". "
                "Alternative: use kaos-tabular-sample to peek at rows, "
                "or kaos-tabular-query for free-form SQL."
            )
            raise EngineError(msg)
        sql_parts = [f"SELECT * FROM {_quote_ident(table_name)} WHERE {where}"]
        if limit is not None:
            if limit < 1:
                msg = (
                    f"filter(limit={limit}): must be >= 1. "
                    "How to fix: omit limit= for unbounded results."
                )
                raise EngineError(msg)
            sql_parts.append(f"LIMIT {int(limit)}")
        sql = " ".join(sql_parts)

        if target is not None:
            ddl = f"CREATE OR REPLACE TABLE {_quote_ident(target)} AS {sql}"
            try:
                self._con.execute(ddl)
            except duckdb.Error as exc:
                msg = (
                    f"filter failed for {table_name!r}: {exc}. "
                    "How to fix: verify the where= predicate is valid "
                    "DuckDB SQL and references real columns. "
                    "Alternative: call describe_table() for the schema."
                )
                raise EngineError(msg) from exc
            if target not in self._registered:
                self._registered.append(target)
            self._record("query", f"filter:{table_name}->{target}", (table_name, target))
            return _bridge_query(
                self._con,
                f"SELECT * FROM {_quote_ident(target)}",
                name=target,
            )

        try:
            result = _bridge_query(self._con, sql, name=f"{table_name}_filter")
        except duckdb.Error as exc:
            msg = (
                f"filter failed for {table_name!r}: {exc}. "
                "How to fix: verify the where= predicate is valid "
                "DuckDB SQL and references real columns. "
                "Alternative: call describe_table() for the schema."
            )
            raise EngineError(msg) from exc
        self._record("query", f"filter:{table_name}", (table_name,))
        return result

    def top_k(
        self,
        table_name: str,
        *,
        by: str | Sequence[str],
        n: int = 10,
        ascending: bool = False,
        target: str | None = None,
    ) -> Table:
        """Return the top ``n`` rows ordered by ``by`` columns.

        ``ascending`` defaults to ``False`` because "top N by column"
        almost always means largest-first; pass ``ascending=True`` for
        smallest-first (i.e. bottom-N).

        ``by`` may be a single column name or a sequence — the order
        applies to every column in turn. All ``by`` columns must exist
        on ``table_name``.
        """
        self._assert_table_exists(table_name, op="top_k")
        if n < 1:
            msg = (
                f"top_k(n={n}): n must be >= 1. "
                "How to fix: pass n=10 (default) or any positive integer."
            )
            raise EngineError(msg)
        by_list: list[str] = [by] if isinstance(by, str) else list(by)
        if not by_list:
            msg = (
                "top_k(by=[]): at least one column to order by is required. "
                "How to fix: pass by='units' or by=['region', 'units']. "
                "Alternative: use kaos-tabular-sample for a random sample."
            )
            raise EngineError(msg)
        self._assert_columns_exist(table_name, by_list, op="top_k(by=)")

        direction = "ASC" if ascending else "DESC"
        ob_csv = ", ".join(f"{_quote_ident(c)} {direction}" for c in by_list)
        sql = f"SELECT * FROM {_quote_ident(table_name)} ORDER BY {ob_csv} LIMIT {int(n)}"

        if target is not None:
            ddl = f"CREATE OR REPLACE TABLE {_quote_ident(target)} AS {sql}"
            try:
                self._con.execute(ddl)
            except duckdb.Error as exc:
                msg = (
                    f"top_k failed for {table_name!r}: {exc}. "
                    "How to fix: verify every column in by= exists and "
                    "is sortable. Alternative: describe_table() for the schema."
                )
                raise EngineError(msg) from exc
            if target not in self._registered:
                self._registered.append(target)
            self._record("query", f"top_k:{table_name}->{target}", (table_name, target))
            return _bridge_query(
                self._con,
                f"SELECT * FROM {_quote_ident(target)}",
                name=target,
            )

        try:
            result = _bridge_query(self._con, sql, name=f"{table_name}_top_k")
        except duckdb.Error as exc:
            msg = (
                f"top_k failed for {table_name!r}: {exc}. "
                "How to fix: verify every column in by= exists and "
                "is sortable. Alternative: describe_table() for the schema."
            )
            raise EngineError(msg) from exc
        self._record("query", f"top_k:{table_name}", (table_name,))
        return result

    # -- Multi-table operations -------------------------------------------

    _JOIN_TYPES: tuple[str, ...] = (
        "inner",
        "left",
        "right",
        "outer",
        "semi",
        "anti",
        "cross",
    )

    def join(
        self,
        left: str,
        right: str,
        *,
        on: str | list[str] | None = None,
        how: str = "inner",
        target: str | None = None,
    ) -> Table:
        """SQL JOIN two registered tables.

        Uses DuckDB's ``USING (col)`` clause so the join key appears once in
        the result (no ``l.x``/``r.x`` ambiguity). For ``how='cross'`` the
        ``on`` argument must be ``None``; for every other join type ``on``
        is required.

        If ``target`` is set the result is materialized as
        ``CREATE OR REPLACE TABLE <target>`` and registered with the engine;
        otherwise the result is computed but not persisted.
        """
        how_lower = how.lower()
        if how_lower not in self._JOIN_TYPES:
            msg = (
                f"join(how={how!r}): not one of {list(self._JOIN_TYPES)}. "
                "How to fix: pass how='inner' / 'left' / 'right' / 'outer' / "
                "'semi' / 'anti' / 'cross'."
            )
            raise EngineError(msg)
        self._assert_table_exists(left, op="join(left)")
        self._assert_table_exists(right, op="join(right)")
        if how_lower == "cross":
            if on is not None:
                msg = "join(how='cross') must not pass on=; cross-joins have no key."
                raise EngineError(msg)
            join_clause = f"{_quote_ident(left)} CROSS JOIN {_quote_ident(right)}"
        else:
            if on is None:
                msg = (
                    f"join(how={how!r}) requires on= (column name or list of names). "
                    "How to fix: pass on='id' or on=['col_a', 'col_b']."
                )
                raise EngineError(msg)
            on_list = [on] if isinstance(on, str) else list(on)
            if not on_list:
                msg = "join(on=[]): at least one column is required."
                raise EngineError(msg)
            self._assert_columns_exist(left, on_list, op=f"join(on=, table={left!r})")
            self._assert_columns_exist(right, on_list, op=f"join(on=, table={right!r})")
            using_csv = ", ".join(_quote_ident(c) for c in on_list)
            join_word = how_lower.upper() + (" OUTER" if how_lower == "outer" else "")
            join_clause = (
                f"{_quote_ident(left)} {join_word} JOIN {_quote_ident(right)} USING ({using_csv})"
            )
        select_sql = f"SELECT * FROM {join_clause}"

        if target is not None:
            ddl = f"CREATE OR REPLACE TABLE {_quote_ident(target)} AS {select_sql}"
            try:
                self._con.execute(ddl)
            except duckdb.Error as exc:
                msg = (
                    f"join failed: {exc}. How to fix: verify both tables are "
                    f"registered and share the join column(s). Alternative: call "
                    "list_tables() to see what's registered."
                )
                raise EngineError(msg) from exc
            if target not in self._registered:
                self._registered.append(target)
            self._record("query", f"join:{left}+{right}->{target}", (left, right, target))
            return _bridge_query(self._con, f"SELECT * FROM {_quote_ident(target)}", name=target)

        try:
            result = _bridge_query(self._con, select_sql, name=f"{left}_join_{right}")
        except duckdb.Error as exc:
            msg = (
                f"join failed: {exc}. How to fix: verify both tables are "
                f"registered and share the join column(s). Alternative: call "
                "list_tables() to see what's registered."
            )
            raise EngineError(msg) from exc
        self._record("query", f"join:{left}+{right}", (left, right))
        return result

    _PIVOT_AGGREGATES: tuple[str, ...] = ("sum", "avg", "min", "max", "count", "first")

    def pivot(
        self,
        table_name: str,
        *,
        on: str,
        using: str,
        aggregate: str = "sum",
        group_by: str | list[str] | None = None,
        target: str | None = None,
    ) -> Table:
        """Pivot a long-form table into wide form via DuckDB ``PIVOT``."""
        agg_lower = aggregate.lower()
        if agg_lower not in self._PIVOT_AGGREGATES:
            msg = (
                f"pivot(aggregate={aggregate!r}): not one of "
                f"{list(self._PIVOT_AGGREGATES)}. How to fix: pass aggregate='sum' "
                "(or avg/min/max/count/first)."
            )
            raise EngineError(msg)
        self._assert_table_exists(table_name, op="pivot")
        self._assert_columns_exist(table_name, [on, using], op="pivot")
        gb_clause = ""
        if group_by is not None:
            gb_list = [group_by] if isinstance(group_by, str) else list(group_by)
            if gb_list:
                self._assert_columns_exist(table_name, gb_list, op="pivot(group_by=)")
                gb_csv = ", ".join(_quote_ident(c) for c in gb_list)
                gb_clause = f" GROUP BY {gb_csv}"
        sql = (
            f"PIVOT {_quote_ident(table_name)} "
            f"ON {_quote_ident(on)} "
            f"USING {agg_lower}({_quote_ident(using)})"
            f"{gb_clause}"
        )

        if target is not None:
            ddl = f"CREATE OR REPLACE TABLE {_quote_ident(target)} AS {sql}"
            try:
                self._con.execute(ddl)
            except duckdb.Error as exc:
                msg = (
                    f"pivot failed: {exc}. How to fix: verify {on!r} and "
                    f"{using!r} exist on {table_name!r}. Alternative: "
                    "describe_table() to inspect the schema."
                )
                raise EngineError(msg) from exc
            if target not in self._registered:
                self._registered.append(target)
            self._record("query", f"pivot:{table_name}->{target}", (table_name, target))
            return _bridge_query(self._con, f"SELECT * FROM {_quote_ident(target)}", name=target)

        try:
            result = _bridge_query(self._con, sql, name=f"{table_name}_pivot")
        except duckdb.Error as exc:
            msg = (
                f"pivot failed: {exc}. How to fix: verify {on!r} and "
                f"{using!r} exist on {table_name!r}. Alternative: "
                "describe_table() to inspect the schema."
            )
            raise EngineError(msg) from exc
        self._record("query", f"pivot:{table_name}", (table_name,))
        return result

    def unpivot(
        self,
        table_name: str,
        *,
        columns: list[str],
        name_column: str = "variable",
        value_column: str = "value",
        target: str | None = None,
    ) -> Table:
        """Unpivot wide-form columns into long form via DuckDB ``UNPIVOT``."""
        if not columns:
            msg = (
                "unpivot(columns=[]): at least one column to melt is required. "
                "How to fix: pass columns=['col1', 'col2', ...]."
            )
            raise EngineError(msg)
        self._assert_table_exists(table_name, op="unpivot")
        self._assert_columns_exist(table_name, columns, op="unpivot")
        on_csv = ", ".join(_quote_ident(c) for c in columns)
        sql = (
            f"UNPIVOT {_quote_ident(table_name)} "
            f"ON {on_csv} "
            f"INTO NAME {_quote_ident(name_column)} "
            f"VALUE {_quote_ident(value_column)}"
        )

        if target is not None:
            ddl = f"CREATE OR REPLACE TABLE {_quote_ident(target)} AS {sql}"
            try:
                self._con.execute(ddl)
            except duckdb.Error as exc:
                msg = (
                    f"unpivot failed: {exc}. How to fix: verify all of "
                    f"{columns!r} exist on {table_name!r} and share a "
                    "compatible type. Alternative: describe_table() to inspect."
                )
                raise EngineError(msg) from exc
            if target not in self._registered:
                self._registered.append(target)
            self._record("query", f"unpivot:{table_name}->{target}", (table_name, target))
            return _bridge_query(self._con, f"SELECT * FROM {_quote_ident(target)}", name=target)

        try:
            result = _bridge_query(self._con, sql, name=f"{table_name}_unpivot")
        except duckdb.Error as exc:
            msg = (
                f"unpivot failed: {exc}. How to fix: verify all of "
                f"{columns!r} exist on {table_name!r} and share a "
                "compatible type. Alternative: describe_table() to inspect."
            )
            raise EngineError(msg) from exc
        self._record("query", f"unpivot:{table_name}", (table_name,))
        return result

    def to_tabular_document(
        self,
        table_name: str,
        *,
        max_rows: int | None = None,
    ) -> TabularDocument:
        """Convert a registered table to a TabularDocument.

        Args:
            table_name: The table to convert.
            max_rows: Maximum rows to include. None = all rows.

        Returns:
            TabularDocument with one table.
        """
        sql = f"SELECT * FROM {_quote_ident(table_name)}"
        if max_rows is not None:
            sql += f" LIMIT {min(max_rows, _MAX_ROWS_HARD_CAP)}"
        table = _bridge_query(self._con, sql, name=table_name)

        # Get full row count
        total = self.count(table_name)
        if total != table.row_count:
            table = Table(
                name=table.name,
                columns=table.columns,
                rows=table.rows,
                row_count=total,
                metadata=table.metadata,
            )

        return TabularDocument(
            metadata=DocumentMetadata(title=table_name),
            tables=(table,),
        )

    # -- Persistence -------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Export the full database state to a directory.

        For in-memory engines, this is how you persist session state.
        The exported directory can be imported into a new connection
        via DuckDB's ``IMPORT DATABASE``.

        Args:
            path: Directory path for the export.
        """
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        # Resolve to an absolute path before escaping, then route
        # through ``_q_lit``. DuckDB does not offer parameter binding
        # for EXPORT/COPY target paths, so the single-quote escape is
        # the load-bearing mitigation against a caller-supplied path
        # that contains a quote (post-release-review #2).
        self._con.execute(f"EXPORT DATABASE {_q_lit(str(p.resolve()))}")

    def export_table(
        self,
        table_name: str,
        output_path: str | Path,
        *,
        format: Literal["csv", "parquet", "json"],
    ) -> int:
        """Export a registered table to a file.

        Public API replacing prior callers that reached into ``engine._con``
        directly (audit-01 KTAB-003). Owns DuckDB ``COPY`` SQL, format
        mapping, and path quoting.

        Args:
            table_name: Name of a registered table.
            output_path: Destination file path. Parent directories are
                expected to exist.
            format: One of ``csv``, ``parquet``, ``json``. Callers that
                want extension-driven inference should resolve it
                themselves before calling.

        Returns:
            The row count of the exported table.

        Raises:
            EngineError: if the table is not registered or DuckDB rejects
                the COPY statement.
        """
        duckdb_fmt = {"csv": "CSV", "parquet": "PARQUET", "json": "JSON"}.get(format)
        if duckdb_fmt is None:
            msg = f"Unsupported export format: {format!r}. Choose one of: csv, parquet, json."
            raise EngineError(msg)
        self._assert_table_exists(table_name, op="export_table")

        # SQL-quote the identifier and escape the path. DuckDB does not
        # offer parameter binding for COPY-target paths, so the
        # single-quote escape (``_q_lit``) is the load-bearing
        # mitigation.
        quoted_ident = _quote_ident(table_name)
        out = _q_lit(str(Path(output_path).resolve()))

        try:
            self._con.execute(f"COPY {quoted_ident} TO {out} (FORMAT {duckdb_fmt})")
        except duckdb.Error as exc:
            msg = (
                f"Export failed for table {table_name!r}: {exc}. "
                f"Use list_tables() to see registered tables; "
                f"register the source first via register_file()."
            )
            raise EngineError(msg) from exc

        row_count = self.count(table_name)
        self._record("query", f"export:{table_name}", (table_name,))
        return row_count

    def close(self) -> None:
        """Close the DuckDB connection and release resources."""
        import contextlib

        with contextlib.suppress(Exception):
            self._con.close()

    # -- History -----------------------------------------------------------

    def history(self, *, last_n: int = 20) -> list[EngineEvent]:
        """Return the most recent engine events."""
        return self._history[-last_n:]

    def undo_last_register(self) -> str | None:
        """Drop the most recently registered table.

        Returns:
            The dropped table name, or None if nothing to undo.
        """
        if not self._registered:
            return None
        name = self._registered.pop()
        self._con.execute(f"DROP TABLE IF EXISTS {_quote_ident(name)}")
        self._record("drop", name, (name,))
        return name

    # -- Internal ----------------------------------------------------------

    def _record(
        self,
        event_type: Literal["register", "query", "drop"],
        detail: str,
        table_names: tuple[str, ...],
    ) -> None:
        self._history.append(
            EngineEvent(
                timestamp=datetime.datetime.now(tz=datetime.UTC),
                event_type=event_type,
                detail=detail,
                table_names=table_names,
            )
        )
