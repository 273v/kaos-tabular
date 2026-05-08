"""MCP tools for kaos-tabular: 8 tools for tabular data operations.

Tools follow the kaos-core KaosTool ABC pattern with explicit ToolAnnotations,
three-part error messages, and flat parameter schemas.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from kaos_core.base.tool import KaosTool
from kaos_core.logging import get_logger
from kaos_core.types.annotations import ToolAnnotations
from kaos_core.types.enums import ToolCapability, ToolCategory
from kaos_core.types.metadata import ToolMetadata
from kaos_core.types.parameters import ParameterSchema
from kaos_core.types.results import ToolResult

if TYPE_CHECKING:
    from kaos_core.base.context import KaosContext
    from kaos_core.registry.container import KaosRuntime

from kaos_tabular._session import SESSION_REGISTRY
from kaos_tabular.engine import TabularEngine
from kaos_tabular.errors import EngineError

logger = get_logger(__name__)

_MODULE = "kaos-tabular"
_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Export format type + resolution helpers
# ---------------------------------------------------------------------------

ExportFormat = Literal["csv", "parquet", "json"]


def _coerce_export_format(value: Any) -> ExportFormat | None:
    """Validate ``value`` is one of the literal export formats.

    Returning a ``Literal`` directly (rather than narrowing via
    ``isinstance`` or membership) lets ty see the narrow without a
    ``cast``. Used by ``ExportTool`` to validate the optional
    ``format`` MCP argument.
    """
    if value == "csv":
        return "csv"
    if value == "parquet":
        return "parquet"
    if value == "json":
        return "json"
    return None


def _infer_export_format_from_extension(ext: str) -> ExportFormat | None:
    """Map a file extension to one of the three export formats."""
    if ext == ".csv":
        return "csv"
    if ext in (".parquet", ".pq"):
        return "parquet"
    if ext == ".json":
        return "json"
    return None


# Annotation contract: every tool below builds its own
# ``ToolAnnotations`` literal in its ``metadata`` property. We
# intentionally do not factor a shared module-level constant — that
# pattern silently swept the wrong values onto half the tools in
# pre-release-review revisions. Owning the literal at the tool boundary
# eliminates the misclassification class entirely.
#
# Three shapes appear below:
#   - closed-world read (``openWorldHint=False``): metadata-only —
#     ListTables / Describe / Sample / Count. The SQL these run is
#     constructed internally and cannot reach the filesystem.
#   - open-world read (``openWorldHint=True``, not destructive):
#     Register / ReadFile take a filesystem path; Query takes free-form
#     DuckDB SQL which can call ``read_csv_auto`` / ``read_parquet`` /
#     ``read_json_auto`` on any file the process can read.
#   - destructive write (``destructiveHint=True``, ``openWorldHint=True``):
#     Export writes / overwrites a caller-supplied path.
# See ``docs/security.md`` for the full trust contract.

# ---------------------------------------------------------------------------
# Session engine bridge
# ---------------------------------------------------------------------------
#
# The actual cache lives in :mod:`kaos_tabular._session`. This thin
# wrapper exists so each tool's ``execute`` keeps the simple
# ``engine = await _get_engine(context)`` shape — the alternative
# (every tool reaching into ``SESSION_REGISTRY`` directly) leaks the
# ``context is None`` ephemeral-engine policy across eight call sites.


async def _get_engine(context: KaosContext | None) -> TabularEngine:
    """Resolve the engine for a tool invocation.

    With a context, returns the session-scoped engine from the
    process-wide :data:`SESSION_REGISTRY` (creating it on first use,
    LRU-evicting the oldest session past capacity). Without a
    context, returns a fresh ephemeral engine the caller is
    responsible for closing.
    """
    if context is None:
        return TabularEngine()
    return await SESSION_REGISTRY.get(context.session_id)


# ---------------------------------------------------------------------------
# Tool 1: Register
# ---------------------------------------------------------------------------


class RegisterTool(KaosTool):
    """Register a data file as a queryable table."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-register",
            display_name="Register Table",
            description=(
                "Register a CSV, Parquet, or JSON file as a queryable table. "
                "After registration, use kaos-tabular-query to run SQL against it. "
                "DuckDB handles type inference and format detection automatically."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.EXTRACT,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
            ),
            input_schema=[
                ParameterSchema(
                    name="path",
                    type="string",
                    description="Path to the data file (CSV, TSV, Parquet, JSON, JSONL).",
                ),
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name for the table. Defaults to the filename stem.",
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]
        table_name = inputs.get("table_name")

        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                f"How to fix: pass an absolute path or one relative to the runtime's "
                f"working directory; verify the file exists. "
                f"Alternative: use kaos-tabular-list-tables to see what is already registered."
            )

        try:
            engine = await _get_engine(context)
            name = engine.register_file(path, table_name=table_name)
            desc = engine.describe_table(name)

            return ToolResult.create_success(
                output={
                    "table_name": name,
                    "row_count": desc["row_count"],
                    "column_count": desc["column_count"],
                    "columns": desc["columns"],
                    "message": (
                        f"Registered '{name}' ({desc['row_count']} rows, "
                        f"{desc['column_count']} columns). "
                        "Use kaos-tabular-query to run SQL against it."
                    ),
                }
            )
        except ValueError as exc:
            return ToolResult.create_error(
                f"Failed to register file {path!r}: {exc}. "
                f"How to fix: verify the file exists and is one of the supported "
                f"formats — CSV, TSV, Parquet, JSON, JSONL, SQLite. "
                f"For XLSX files, parse with kaos_office.parse_xlsx() and pass each "
                f"Table to TabularEngine.register_table(). "
                f"Alternative: use kaos-tabular-list-tables to see currently registered tables."
            )
        except Exception as exc:
            return ToolResult.create_error(
                f"Failed to register '{Path(path).name}': {exc}. "
                "The file may be corrupted or in an unsupported format. "
                "Supported: CSV, TSV, Parquet, JSON, JSONL."
            )


# ---------------------------------------------------------------------------
# Tool 2: Query
# ---------------------------------------------------------------------------


class QueryTool(KaosTool):
    """Execute SQL against registered tables."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-query",
            display_name="Query Table",
            description=(
                "Execute arbitrary DuckDB SQL against the session's in-process "
                "engine. Returns results as TSV. Use kaos-tabular-register first "
                "to register data files; use kaos-tabular-describe to inspect "
                "schema. SQL has filesystem access matching the running process — "
                "for stricter isolation, run kaos-tabular in a constrained working "
                "directory or container. See docs/security.md for the full trust "
                "model."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
            ),
            input_schema=[
                ParameterSchema(
                    name="sql",
                    type="string",
                    description="SQL query to execute.",
                ),
                ParameterSchema(
                    name="max_rows",
                    type="integer",
                    description="Maximum rows to return. Default 1000, max 10000.",
                    required=False,
                    default=1000,
                    constraints={"min": 1, "max": 10000},
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        sql = inputs["sql"]
        max_rows = inputs.get("max_rows", 1000)

        engine = await _get_engine(context)
        tables = engine.list_tables()
        if not tables:
            return ToolResult.create_error(
                "No tables registered. "
                "How to fix: call kaos-tabular-register with a CSV / Parquet / JSON / "
                "JSONL / SQLite path first. "
                "Alternative: use kaos-tabular-read-file to register and snapshot a "
                "file in one step (creates a session-scoped artifact)."
            )

        try:
            from kaos_content.serializers.tabular import serialize_tsv

            result = engine.execute(sql, max_rows=max_rows)
            tsv = serialize_tsv(result)

            return ToolResult.create_text(
                f"({len(result.rows)} rows, {len(result.columns)} columns)\n\n{tsv}"
            )
        except Exception as exc:
            available = ", ".join(t["name"] for t in tables)
            return ToolResult.create_error(
                f"SQL error: {exc}. "
                f"Available tables: {available}. "
                "Use kaos-tabular-describe to inspect table schema."
            )


# ---------------------------------------------------------------------------
# Tool 3: List Tables
# ---------------------------------------------------------------------------


class ListTablesTool(KaosTool):
    """List all registered tables."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-list-tables",
            display_name="List Tables",
            description=(
                "List all registered tables with their row counts and column counts. "
                "Use after kaos-tabular-register to see available tables."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        engine = await _get_engine(context)
        tables = engine.list_tables()
        return ToolResult.create_success(output={"tables": tables, "count": len(tables)})


# ---------------------------------------------------------------------------
# Tool 4: Describe
# ---------------------------------------------------------------------------


class DescribeTool(KaosTool):
    """Describe a table's schema and statistics."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-describe",
            display_name="Describe Table",
            description=(
                "Show column names, types, row count, and sample values for a table. "
                "Use this before kaos-tabular-query to understand the schema."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the table to describe.",
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        engine = await _get_engine(context)

        try:
            desc = engine.describe_table(table_name)
            return ToolResult.create_success(output=desc)
        except Exception as exc:
            tables = engine.list_tables()
            available = ", ".join(t["name"] for t in tables) if tables else "(none)"
            return ToolResult.create_error(
                f"Table '{table_name}' not found: {exc}. "
                f"Available tables: {available}. "
                "Use kaos-tabular-list-tables to see all tables."
            )


# ---------------------------------------------------------------------------
# Tool 5: Sample
# ---------------------------------------------------------------------------


class SampleTool(KaosTool):
    """Show sample rows from a table."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-sample",
            display_name="Sample Table",
            description=(
                "Return a random sample of rows from a table as a markdown table. "
                "Useful for quickly inspecting data before writing queries."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the table to sample.",
                ),
                ParameterSchema(
                    name="n",
                    type="integer",
                    description="Number of rows to sample. Default 5.",
                    required=False,
                    default=5,
                    constraints={"min": 1, "max": 100},
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        n = inputs.get("n", 5)
        engine = await _get_engine(context)

        try:
            from kaos_content.serializers.tabular import serialize_markdown_table

            result = engine.sample(table_name, n=n)
            md = serialize_markdown_table(result, max_rows=0)
            return ToolResult.create_text(md)
        except Exception as exc:
            tables = engine.list_tables()
            available = ", ".join(t["name"] for t in tables) if tables else "(none)"
            return ToolResult.create_error(
                f"Failed to sample {table_name!r}: {exc}. "
                f"How to fix: pick one of the available tables: {available}. "
                f"Alternative: use kaos-tabular-describe to inspect schema before sampling, "
                f"or kaos-tabular-list-tables to see registered tables."
            )


# ---------------------------------------------------------------------------
# Tool 6: Count
# ---------------------------------------------------------------------------


class CountTool(KaosTool):
    """Get the row count for a table."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-count",
            display_name="Count Rows",
            description=(
                "Return the total row count for a table. "
                "Faster than running SELECT COUNT(*) via kaos-tabular-query."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the table.",
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        engine = await _get_engine(context)

        try:
            count = engine.count(table_name)
            return ToolResult.create_success(output={"table_name": table_name, "row_count": count})
        except Exception as exc:
            return ToolResult.create_error(
                f"Failed to count rows in '{table_name}': {exc}. "
                "Use kaos-tabular-list-tables to see available tables."
            )


# ---------------------------------------------------------------------------
# Tool 7: Read File
# ---------------------------------------------------------------------------


class ReadFileTool(KaosTool):
    """Read a data file into a TabularDocument artifact."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-read-file",
            display_name="Read Data File",
            description=(
                "Read a CSV, Parquet, or JSON file and store as a TabularDocument artifact. "
                "Returns the artifact ID for further operations. "
                "Use kaos-tabular-register if you want to query with SQL instead."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.EXTRACT,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
            ),
            input_schema=[
                ParameterSchema(
                    name="path",
                    type="string",
                    description="Path to the data file.",
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]

        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                f"How to fix: pass an absolute path or one relative to the runtime's "
                f"working directory; verify the file exists. "
                f"Alternative: use kaos-tabular-list-tables to see what is already registered."
            )

        if context is None or context.runtime is None:
            return ToolResult.create_error(
                "No runtime context available. "
                "ReadFile requires a KaosRuntime with artifact storage. "
                "Use kaos-tabular-register for context-free operations."
            )

        try:
            from kaos_content.artifacts import store_tabular
            from kaos_content.serializers.tabular import serialize_tabular_summary

            from kaos_tabular.readers import _read_file

            doc = _read_file(path)
            manifest = await store_tabular(
                doc,
                context.runtime,
                context,
                name=Path(path).stem,
                description=f"Read from {Path(path).name}",
            )
            summary = serialize_tabular_summary(doc)

            return manifest.to_tool_result(
                summary=summary,
                structured_content={
                    "artifact_id": manifest.artifact_id,
                    "table_count": len(doc.tables),
                    "total_rows": sum(t.row_count for t in doc.tables),
                    "tables": [
                        {"name": t.name, "row_count": t.row_count, "columns": len(t.columns)}
                        for t in doc.tables
                    ],
                },
            )
        except Exception as exc:
            return ToolResult.create_error(
                f"Failed to read {Path(path).name!r}: {exc}. "
                f"How to fix: verify the file is non-empty and matches the supported "
                f"formats (CSV, TSV, Parquet, JSON, JSONL, SQLite); for XLSX use "
                f"kaos_office.parse_xlsx() then engine.register_table(). "
                f"Alternative: use kaos-tabular-register for a one-shot register-only "
                f"flow (no artifact snapshot)."
            )


# ---------------------------------------------------------------------------
# Tool 8: Export
# ---------------------------------------------------------------------------


class ExportTool(KaosTool):
    """Export a registered table to a file."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-export",
            display_name="Export Table",
            description=(
                "Export a registered table to a CSV, Parquet, or JSON file. "
                "Register the source data first with kaos-tabular-register."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.TRANSFORM,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the table to export.",
                ),
                ParameterSchema(
                    name="output_path",
                    type="string",
                    description="Path for the output file.",
                ),
                ParameterSchema(
                    name="format",
                    type="string",
                    description="Output format. Default: infer from file extension.",
                    required=False,
                    constraints={"enum": ["csv", "parquet", "json"]},
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        output_path = inputs["output_path"]

        engine = await _get_engine(context)

        # Resolve the export format. Explicit ``format`` arg takes
        # priority; falls back to extension inference. Both helpers
        # return ``ExportFormat | None`` so ty sees the narrow
        # without a cast.
        fmt: ExportFormat | None = _coerce_export_format(inputs.get("format"))
        if fmt is None:
            ext = Path(output_path).suffix.lower()
            fmt = _infer_export_format_from_extension(ext)
        if fmt is None:
            ext = Path(output_path).suffix.lower()
            return ToolResult.create_error(
                f"Cannot infer export format from extension {ext!r}. "
                f"How to fix: pass the format parameter explicitly — "
                f"one of 'csv', 'parquet', or 'json'. "
                f"Alternative: rename the output path to use a "
                f".csv / .parquet / .json extension."
            )

        try:
            row_count = engine.export_table(table_name, output_path, format=fmt)
        except EngineError as exc:
            return ToolResult.create_error(str(exc))

        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "output_path": output_path,
                "format": fmt,
                "row_count": row_count,
                "message": f"Exported {row_count} rows to {output_path}",
            }
        )


# ---------------------------------------------------------------------------
# Tool 9: History
# ---------------------------------------------------------------------------


class HistoryTool(KaosTool):
    """Return the recent engine event history for the session."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-history",
            display_name="Engine History",
            description=(
                "Return the recent engine event history for the session — registered "
                "files, executed queries, dropped tables. Use to retrace a session's "
                "steps or check what data the agent has loaded."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="last_n",
                    type="integer",
                    description="Number of most recent events to return. Default 20.",
                    required=False,
                    default=20,
                    constraints={"minimum": 1, "maximum": 1000},
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        last_n = int(inputs.get("last_n", 20))
        engine = await _get_engine(context)
        events = engine.history(last_n=last_n)
        serialized = [
            {
                "timestamp": e.timestamp.isoformat(),
                "event_type": e.event_type,
                "detail": e.detail,
                "table_names": list(e.table_names),
            }
            for e in events
        ]
        return ToolResult.create_success(
            output={"events": serialized, "count": len(serialized)},
        )


# ---------------------------------------------------------------------------
# Tool 10: Find Duplicates
# ---------------------------------------------------------------------------


class FindDuplicatesTool(KaosTool):
    """Find rows that share their key with at least one other row."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-find-duplicates",
            display_name="Find Duplicate Rows",
            description=(
                "Return rows in a registered table whose values in the given columns "
                "appear in more than one row. With no columns specified, all columns "
                "are used (full-row duplicates). Use to audit a table for unique-key "
                "violations or accidental row repeats."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.ANALYZE,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the registered table.",
                ),
                ParameterSchema(
                    name="columns",
                    type="array",
                    description=(
                        "Columns to consider when grouping rows. Defaults to all columns "
                        "(full-row duplicates)."
                    ),
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        columns = inputs.get("columns")
        engine = await _get_engine(context)
        try:
            result = engine.find_duplicates(table_name, columns=columns)
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "columns": columns,
                "duplicate_row_count": result.row_count,
                "rows": [list(row) for row in result.rows],
                "column_names": [c.name for c in result.columns],
            }
        )


# ---------------------------------------------------------------------------
# Tool 11: Correlation
# ---------------------------------------------------------------------------


class CorrelationTool(KaosTool):
    """Compute pairwise Pearson correlations between numeric columns."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-correlation",
            display_name="Column Correlation",
            description=(
                "Compute pairwise Pearson correlation between numeric columns of a "
                "registered table. Returns long-form (col_a, col_b, corr) rows. With "
                "no columns specified, every numeric column in the table is included."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.ANALYZE,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the registered table.",
                ),
                ParameterSchema(
                    name="columns",
                    type="array",
                    description=(
                        "Numeric columns to correlate. Defaults to every numeric column "
                        "in the table."
                    ),
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        columns = inputs.get("columns")
        engine = await _get_engine(context)
        try:
            result = engine.correlation(table_name, columns=columns)
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "rows": [list(row) for row in result.rows],
                "column_names": [c.name for c in result.columns],
            }
        )


# ---------------------------------------------------------------------------
# Tool 12: Join
# ---------------------------------------------------------------------------


class JoinTool(KaosTool):
    """SQL JOIN two registered tables on shared key columns."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-join",
            display_name="Join Tables",
            description=(
                "JOIN two registered tables on shared key columns. The join uses "
                "DuckDB's USING clause so the join key appears once in the result. "
                "Use this rather than free-form SQL when you can — typed inputs prevent "
                "the column-ambiguity errors that catch agents writing JOIN ON l.x = "
                "r.x by hand."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.TRANSFORM,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="left",
                    type="string",
                    description="Left table name.",
                ),
                ParameterSchema(
                    name="right",
                    type="string",
                    description="Right table name.",
                ),
                ParameterSchema(
                    name="on",
                    type="array",
                    description=(
                        "Column name(s) shared by both tables. Required unless how='cross'."
                    ),
                    required=False,
                ),
                ParameterSchema(
                    name="how",
                    type="string",
                    description="Join type. Default 'inner'.",
                    required=False,
                    default="inner",
                    constraints={
                        "enum": ["inner", "left", "right", "outer", "semi", "anti", "cross"]
                    },
                ),
                ParameterSchema(
                    name="target",
                    type="string",
                    description=(
                        "Optional name to register the result under. If omitted the "
                        "result is returned but not persisted."
                    ),
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        left = inputs["left"]
        right = inputs["right"]
        on = inputs.get("on")
        how = inputs.get("how", "inner")
        target = inputs.get("target")
        engine = await _get_engine(context)
        try:
            result = engine.join(left, right, on=on, how=how, target=target)
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "left": left,
                "right": right,
                "how": how,
                "target": target,
                "row_count": result.row_count,
                "column_names": [c.name for c in result.columns],
            }
        )


# ---------------------------------------------------------------------------
# Tool 13: Pivot
# ---------------------------------------------------------------------------


class PivotTool(KaosTool):
    """Pivot a long-form table into wide form via DuckDB PIVOT."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-pivot",
            display_name="Pivot (long → wide)",
            description=(
                "Reshape a long-form table into wide form. The distinct values of "
                "``on`` become columns; ``using`` is aggregated per cell. With "
                "``group_by`` set, one row per group; without, one row total. "
                "Wraps DuckDB's PIVOT statement."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.TRANSFORM,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the registered table.",
                ),
                ParameterSchema(
                    name="on",
                    type="string",
                    description=("Column whose distinct values become the new column headers."),
                ),
                ParameterSchema(
                    name="using",
                    type="string",
                    description="Value column to aggregate per pivot cell.",
                ),
                ParameterSchema(
                    name="aggregate",
                    type="string",
                    description="Aggregation function. Default 'sum'.",
                    required=False,
                    default="sum",
                    constraints={"enum": ["sum", "avg", "min", "max", "count", "first"]},
                ),
                ParameterSchema(
                    name="group_by",
                    type="array",
                    description="Columns to group by. Default: none (single row).",
                    required=False,
                ),
                ParameterSchema(
                    name="target",
                    type="string",
                    description="Optional name to register the pivoted result under.",
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        on = inputs["on"]
        using = inputs["using"]
        aggregate = inputs.get("aggregate", "sum")
        group_by = inputs.get("group_by")
        target = inputs.get("target")
        engine = await _get_engine(context)
        try:
            result = engine.pivot(
                table_name,
                on=on,
                using=using,
                aggregate=aggregate,
                group_by=group_by,
                target=target,
            )
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "target": target,
                "row_count": result.row_count,
                "column_names": [c.name for c in result.columns],
            }
        )


# ---------------------------------------------------------------------------
# Tool 14: Unpivot
# ---------------------------------------------------------------------------


class UnpivotTool(KaosTool):
    """Unpivot a wide-form table into long form via DuckDB UNPIVOT."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-unpivot",
            display_name="Unpivot (wide → long)",
            description=(
                "Melt the listed columns into long form: each row in the result has "
                "the original non-``columns`` keys plus a (name, value) pair drawn "
                "from each of the unpivoted columns. Wraps DuckDB's UNPIVOT statement."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.TRANSFORM,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the registered table.",
                ),
                ParameterSchema(
                    name="columns",
                    type="array",
                    description="Columns to melt into rows. Must be at least one.",
                ),
                ParameterSchema(
                    name="name_column",
                    type="string",
                    description="Name of the new column holding the source column names.",
                    required=False,
                    default="variable",
                ),
                ParameterSchema(
                    name="value_column",
                    type="string",
                    description="Name of the new column holding the corresponding values.",
                    required=False,
                    default="value",
                ),
                ParameterSchema(
                    name="target",
                    type="string",
                    description="Optional name to register the unpivoted result under.",
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        columns = inputs["columns"]
        name_column = inputs.get("name_column", "variable")
        value_column = inputs.get("value_column", "value")
        target = inputs.get("target")
        engine = await _get_engine(context)
        try:
            result = engine.unpivot(
                table_name,
                columns=columns,
                name_column=name_column,
                value_column=value_column,
                target=target,
            )
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "target": target,
                "row_count": result.row_count,
                "column_names": [c.name for c in result.columns],
            }
        )


# ---------------------------------------------------------------------------
# Tool 15: Aggregate
# ---------------------------------------------------------------------------


def _coerce_aggregates(
    raw: Any,
) -> list[tuple[str, str, str | None]]:
    """Translate the MCP ``aggregates`` payload to engine tuples.

    Accepts a list of dicts (the documented shape):

    .. code-block:: json

        [
          {"func": "sum", "column": "units"},
          {"func": "avg", "column": "amount", "alias": "avg_amount"}
        ]

    Raises :class:`EngineError` for shape errors (missing keys / wrong
    type) so :class:`AggregateTool.execute` can surface them as
    structured tool errors. Validation of the actual func/column names
    is the engine's responsibility — keeping the boundary thin.
    """
    if not isinstance(raw, list) or not raw:
        msg = (
            "aggregates must be a non-empty list of "
            "{func, column[, alias]} objects. "
            "How to fix: pass [{'func': 'sum', 'column': 'units'}]. "
            "Alternative: call kaos-tabular-query for free-form aggregate SQL."
        )
        raise EngineError(msg)
    out: list[tuple[str, str, str | None]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            msg = (
                f"aggregates[{i}] must be an object, got {type(entry).__name__}. "
                "How to fix: pass {'func': 'sum', 'column': 'units'}."
            )
            raise EngineError(msg)
        # Annotate as ``dict[str, Any]`` so ty doesn't narrow to
        # ``dict[Never, Never]`` after the isinstance check.
        entry_d = cast(dict[str, Any], entry)
        func = entry_d.get("func")
        col = entry_d.get("column")
        alias = entry_d.get("alias")
        if not isinstance(func, str) or not func:
            msg = f"aggregates[{i}].func must be a non-empty string."
            raise EngineError(msg)
        if not isinstance(col, str) or not col:
            msg = f"aggregates[{i}].column must be a non-empty string."
            raise EngineError(msg)
        if alias is not None and not isinstance(alias, str):
            msg = f"aggregates[{i}].alias must be a string when provided."
            raise EngineError(msg)
        out.append((func, col, alias))
    return out


def _coerce_order_by(raw: Any) -> list[tuple[str, str]] | None:
    """Translate the MCP ``order_by`` payload to engine tuples."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        msg = (
            "order_by must be a list of {column, direction} objects or null. "
            "How to fix: pass [{'column': 'total', 'direction': 'desc'}]."
        )
        raise EngineError(msg)
    out: list[tuple[str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            msg = f"order_by[{i}] must be an object."
            raise EngineError(msg)
        entry_d = cast(dict[str, Any], entry)
        col = entry_d.get("column")
        direction = entry_d.get("direction", "asc")
        if not isinstance(col, str) or not col:
            msg = f"order_by[{i}].column must be a non-empty string."
            raise EngineError(msg)
        if not isinstance(direction, str):
            msg = f"order_by[{i}].direction must be a string ('asc' or 'desc')."
            raise EngineError(msg)
        out.append((col, direction))
    return out


class AggregateTool(KaosTool):
    """Composed GROUP BY with typed validation + did-you-mean error messages."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-aggregate",
            display_name="Aggregate (GROUP BY)",
            description=(
                "Compute aggregate functions over a registered table, optionally "
                "grouped, filtered, ordered, and limited. Prefer this over "
                "kaos-tabular-query for the GROUP BY shape — typed parameters "
                "validate the table, every column, and every aggregate function "
                "before SQL is generated, with did-you-mean suggestions on misses. "
                "The where= and having= clauses are opaque DuckDB SQL fragments "
                "(predicate shapes are unbounded). Use kaos-tabular-describe to "
                "scout the schema first; use kaos-tabular-query for free-form SQL."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.ANALYZE,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the registered table.",
                ),
                ParameterSchema(
                    name="aggregates",
                    type="array",
                    description=(
                        "List of {func, column[, alias]} objects. func must be one of "
                        "sum / avg / min / max / count / count_distinct / median / stddev / "
                        "variance / first / last. column='*' is only legal with func='count'. "
                        "alias names the output column for use in order_by."
                    ),
                ),
                ParameterSchema(
                    name="group_by",
                    type="array",
                    description=(
                        "Columns to GROUP BY. Omit for a single-row aggregation across the "
                        "whole table."
                    ),
                    required=False,
                ),
                ParameterSchema(
                    name="where",
                    type="string",
                    description=(
                        "Optional pre-aggregation predicate, e.g. \"region = 'east'\". "
                        "Opaque DuckDB SQL — verify column names with kaos-tabular-describe."
                    ),
                    required=False,
                ),
                ParameterSchema(
                    name="having",
                    type="string",
                    description=(
                        "Optional post-aggregation predicate referencing an alias, "
                        "e.g. 'total_units > 100'. Requires group_by= to be set."
                    ),
                    required=False,
                ),
                ParameterSchema(
                    name="order_by",
                    type="array",
                    description=(
                        "List of {column, direction} objects. column must be a group_by "
                        "column or an explicit aggregate alias; direction is 'asc' or "
                        "'desc'. Default direction is 'asc'."
                    ),
                    required=False,
                ),
                ParameterSchema(
                    name="limit",
                    type="integer",
                    description="Maximum rows to return after sorting. Default unbounded.",
                    required=False,
                    constraints={"minimum": 1, "maximum": 100000},
                ),
                ParameterSchema(
                    name="target",
                    type="string",
                    description=(
                        "Optional name to register the aggregated result under. If omitted "
                        "the result is computed but not persisted."
                    ),
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        try:
            aggregates = _coerce_aggregates(inputs.get("aggregates"))
            order_by = _coerce_order_by(inputs.get("order_by"))
        except EngineError as exc:
            return ToolResult.create_error(str(exc))

        group_by = inputs.get("group_by")
        where = inputs.get("where")
        having = inputs.get("having")
        limit = inputs.get("limit")
        target = inputs.get("target")
        engine = await _get_engine(context)
        try:
            result = engine.aggregate(
                table_name,
                aggregates=aggregates,
                group_by=group_by,
                where=where,
                having=having,
                order_by=order_by,
                limit=limit,
                target=target,
            )
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "target": target,
                "row_count": result.row_count,
                "column_names": [c.name for c in result.columns],
                "rows": [list(row) for row in result.rows],
            }
        )


# ---------------------------------------------------------------------------
# Tool 16: Filter
# ---------------------------------------------------------------------------


class FilterTool(KaosTool):
    """Apply a typed WHERE filter; opaque DuckDB predicate, validated table."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-filter",
            display_name="Filter (WHERE)",
            description=(
                "Return rows from a registered table that match a WHERE predicate. "
                "The where= clause is opaque DuckDB SQL (predicate shapes are "
                "unbounded — typing them would never end), but the table itself is "
                "validated with did-you-mean on a miss. Use kaos-tabular-describe "
                "to confirm column names before composing predicates; use "
                "kaos-tabular-query for free-form SELECT shapes."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the registered table.",
                ),
                ParameterSchema(
                    name="where",
                    type="string",
                    description=(
                        "DuckDB SQL predicate, e.g. \"region = 'east' AND units > 5\". "
                        "Verify column names with kaos-tabular-describe first."
                    ),
                ),
                ParameterSchema(
                    name="limit",
                    type="integer",
                    description="Maximum rows to return. Default unbounded.",
                    required=False,
                    constraints={"minimum": 1, "maximum": 100000},
                ),
                ParameterSchema(
                    name="target",
                    type="string",
                    description=(
                        "Optional name to register the filtered result under. If omitted "
                        "the result is computed but not persisted."
                    ),
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        where = inputs.get("where", "")
        limit = inputs.get("limit")
        target = inputs.get("target")
        engine = await _get_engine(context)
        try:
            result = engine.filter(table_name, where=where, limit=limit, target=target)
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "target": target,
                "row_count": result.row_count,
                "column_names": [c.name for c in result.columns],
            }
        )


# ---------------------------------------------------------------------------
# Tool 17: Top-K
# ---------------------------------------------------------------------------


class TopKTool(KaosTool):
    """Return the top N rows ordered by one or more columns."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-tabular-top-k",
            display_name="Top K Rows",
            description=(
                "Return the top N rows of a registered table ordered by one or more "
                "columns. Defaults to descending (largest-first); set ascending=true "
                "for smallest-first. Use this rather than free-form ORDER BY ... LIMIT "
                "SQL for the typed validation: every by= column is checked against the "
                "table's schema with did-you-mean on a miss."
            ),
            category=ToolCategory.DATA,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            input_schema=[
                ParameterSchema(
                    name="table_name",
                    type="string",
                    description="Name of the registered table.",
                ),
                ParameterSchema(
                    name="by",
                    type="array",
                    description=(
                        "Column name(s) to order by. Accepts a single string or a list. "
                        "All columns must exist on the table."
                    ),
                ),
                ParameterSchema(
                    name="n",
                    type="integer",
                    description="How many rows to return. Default 10.",
                    required=False,
                    default=10,
                    constraints={"minimum": 1, "maximum": 10000},
                ),
                ParameterSchema(
                    name="ascending",
                    type="boolean",
                    description=(
                        "Sort direction. Default false (largest-first). Set true for "
                        "smallest-first / bottom-N."
                    ),
                    required=False,
                    default=False,
                ),
                ParameterSchema(
                    name="target",
                    type="string",
                    description=(
                        "Optional name to register the result under. If omitted the "
                        "result is computed but not persisted."
                    ),
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        table_name = inputs["table_name"]
        by = inputs.get("by")
        if by is None:
            return ToolResult.create_error(
                "by= is required: pass a column name (string) or list of names. "
                "How to fix: by='units' or by=['region', 'units']. "
                "Alternative: use kaos-tabular-sample for a random sample."
            )
        n = inputs.get("n", 10)
        ascending = bool(inputs.get("ascending", False))
        target = inputs.get("target")
        engine = await _get_engine(context)
        try:
            result = engine.top_k(table_name, by=by, n=n, ascending=ascending, target=target)
        except EngineError as exc:
            return ToolResult.create_error(str(exc))
        return ToolResult.create_success(
            output={
                "table_name": table_name,
                "target": target,
                "row_count": result.row_count,
                "column_names": [c.name for c in result.columns],
                "rows": [list(row) for row in result.rows],
            }
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_tabular_tools(runtime: KaosRuntime) -> int:
    """Register all tabular tools with the runtime. Returns count."""
    tools: list[KaosTool] = [
        RegisterTool(),
        QueryTool(),
        ListTablesTool(),
        DescribeTool(),
        SampleTool(),
        CountTool(),
        ReadFileTool(),
        ExportTool(),
        HistoryTool(),
        FindDuplicatesTool(),
        CorrelationTool(),
        JoinTool(),
        PivotTool(),
        UnpivotTool(),
        AggregateTool(),
        FilterTool(),
        TopKTool(),
    ]
    for tool in tools:
        runtime.tools.register_tool(tool)
    return len(tools)
