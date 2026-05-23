# kaos-tabular

> **Part of [Kelvin Agentic OS](https://kelvin.legal) (KAOS)** — open agentic
> infrastructure for legal work, built by
> [273 Ventures](https://273ventures.com).
> See the [full KAOS package map](https://github.com/273v) for the rest of the stack.

[![PyPI - Version](https://img.shields.io/pypi/v/kaos-tabular)](https://pypi.org/project/kaos-tabular/)
[![Python](https://img.shields.io/pypi/pyversions/kaos-tabular)](https://pypi.org/project/kaos-tabular/)
[![License](https://img.shields.io/pypi/l/kaos-tabular)](https://github.com/273v/kaos-tabular/blob/main/LICENSE)
[![CI](https://github.com/273v/kaos-tabular/actions/workflows/ci.yml/badge.svg)](https://github.com/273v/kaos-tabular/actions/workflows/ci.yml)

`kaos-tabular` is the SQL-analytics layer of the Kelvin Agentic OS (KAOS),
the agentic infrastructure 273 Ventures builds for legal work. It wraps a
session-scoped, in-process [DuckDB](https://duckdb.org) connection in a
typed `TabularEngine` that can register CSV / TSV / Parquet / JSON / JSONL
/ SQLite files and `kaos-content` `TabularDocument` instances, run SQL
against them, and export results back to disk. The engine delegates every
DuckDB call to `kaos_content.bridges.duckdb` — no parser, no executor,
no second copy of the data — and returns typed `Table` / `TabularDocument`
results with provenance instead of raw cursors. Seventeen MCP tools
(`kaos-tabular-{register, query, list-tables, describe, sample, count,
read-file, export, history, find-duplicates, correlation, join, pivot,
unpivot, aggregate, filter, top-k}`) expose the same surface to agentic
clients. Every error message that mentions a missing table, column, or
aggregate function carries a "Did you mean …?" suggestion so an agent
can self-correct without an extra `describe` round-trip. The set is
deliberately bounded — Kelvin's predecessor shipped roughly sixty tools
and porting it forward exposed two distinct reasons a typed tool earns
its weight: it removes a syntactic footgun the agent would otherwise
hit (joins with column ambiguity, PIVOT / UNPIVOT, long-form correlation
matrices), or it adds load-bearing validation and structured-event
audit (the GROUP BY / WHERE / ORDER BY trio: agents write the SQL
correctly but cheap typed wrappers catch column typos at the boundary
and emit replayable history events).

The base install is intentionally small: three runtime dependencies
(`kaos-content`, `kaos-core`, `duckdb`) and no compiled native code
beyond the DuckDB wheel, which ships prebuilt for Linux, macOS, and
Windows on x86_64 and arm64. A
single optional extra, `[mcp]`, adds `kaos-mcp` so `kaos-tabular-serve`
can stand up a stdio or streamable-HTTP server. **There is no `[xlsx]`
extra at 0.1.0 GA** — the previous `_register_xlsx` path introduced a
sideways `kaos-tabular -> kaos-office` dependency that the architecture
DAG forbids, and was removed in audit-01 KTAB-002. The supported XLSX
workflow is now: parse the file with `kaos_office.parse_xlsx(path)` (in
`kaos-office`, the right home for OPC reading) and pass each returned
`Table` to `engine.register_table(table, name=...)` — that method is
already public and unchanged.

## Install

```bash
uv add "kaos-tabular>=0.1.0"
# or
pip install "kaos-tabular>=0.1.0"

# Optional: stdio + streamable-HTTP MCP server entrypoint
uv add 'kaos-tabular[mcp]>=0.1.0'
```

`kaos-tabular` requires Python **3.13** or newer (3.14 is supported).
The package is pure Python, classified `Operating System :: OS
Independent` — DuckDB ships native wheels for the major
platforms, so a clean `pip install` works on Linux, macOS, and Windows
without a compiler toolchain.

## Quick start

Open an in-process engine, register a CSV, run SQL, inspect the schema,
and export the result. `TabularEngine` is a context manager — use `with`
so the DuckDB connection is closed deterministically:

```python
from kaos_tabular import TabularEngine

with TabularEngine() as engine:
    # 1. Register a file. DuckDB infers types, delimiter, and encoding.
    name = engine.register_file("orders.csv", table_name="orders")
    print("Registered as:", name)                      # → "orders"

    # 2. Run SQL. Results come back as a typed kaos-content Table.
    result = engine.execute("SELECT region, SUM(amount) AS total "
                            "FROM orders GROUP BY region")
    print(result.row_count, "rows,", len(result.columns), "columns")

    # 3. Inspect the schema (column names, types, nullability, samples).
    desc = engine.describe_table("orders")
    print(desc["row_count"], "rows in 'orders'")

    # 4. Export — public engine API (audit-01 KTAB-003).
    engine.export_table("orders", "orders.parquet", format="parquet")
```

To expose the same surface to MCP clients, register the seventeen tools
on a `KaosRuntime` and serve them — the easiest path is the `[mcp]`
extra and the bundled `kaos-tabular-serve` entry point, but you can
also wire it into an existing FastMCP app:

```python
from kaos_core import KaosRuntime
from kaos_mcp import KaosMCPServer, KaosMCPSettings

from kaos_tabular.tools import register_tabular_tools

runtime = KaosRuntime()
n_tools = register_tabular_tools(runtime)             # → 17
server = KaosMCPServer(
    runtime=runtime,
    settings=KaosMCPSettings(name="kaos-tabular-server", transport="stdio"),
)
server.run_stdio()                                    # for Claude Code / Codex / Gemini
```

Per-session engines are keyed off `KaosContext.session_id` inside
`tools.py`, so concurrent MCP sessions never share a DuckDB connection.

## Concepts

The package is a thin, typed surface over DuckDB plus the kaos-content
bridges. The most important entries:

| Concept | Purpose |
|---|---|
| **`TabularEngine(db_path=None, read_only=False)`** | Session-scoped wrapper around a single DuckDB connection. Context-manager friendly (`with TabularEngine() as engine:`). `db_path` selects file-backed persistence; in-memory by default. |
| **`engine.register_file(path, *, table_name=None)`** | Register a CSV / TSV / Parquet / JSON / JSONL / SQLite file as a queryable table. Returns the registered name. Multi-table SQLite files are exploded; XLSX is intentionally not handled — see the dependency footprint paragraph. |
| **`engine.register_table(table, *, name=None)`** | Register a `kaos_content` `Table` (e.g. one returned by `kaos_office.parse_xlsx()`) as a DuckDB view. Returns the registered name. |
| **`engine.execute(sql, *, max_rows=1000)`** | Run arbitrary DuckDB SQL. Wraps the user's SQL as `SELECT * FROM (<sql>) AS _q LIMIT N` — the hard cap is 10,000 rows. Returns a typed `Table`. |
| **`engine.describe_table(name)` / `engine.list_tables()` / `engine.count(name)` / `engine.sample(name, n=5)`** | Introspection helpers. `describe_table` returns column metadata, row count, and sample values; `sample` returns a `Table` of N random rows. |
| **`engine.export_table(name, path, *, format)`** | Public engine method (audit-01 KTAB-003, shipped in 0.1.0 GA). Owns the DuckDB `COPY` SQL, format mapping (`csv` / `parquet` / `json`), and path quoting that the export tool and CLI used to reach into private internals for. |
| **`engine.find_duplicates(name, *, columns=None)`** | Return rows whose values in `columns` (default: all columns) appear in more than one row. Uses DuckDB `QUALIFY COUNT(*) OVER (PARTITION BY …) > 1` so the SQL is one statement. |
| **`engine.correlation(name, *, columns=None)`** | Long-form pairwise Pearson correlation matrix — returns `(col_a, col_b, corr)` rows. Default `columns=None` auto-selects every numeric column. |
| **`engine.join(left, right, *, on, how="inner", target=None)`** | SQL JOIN via DuckDB's `USING (col)` clause so the join key appears once in the result. `how` ∈ `{inner, left, right, outer, semi, anti, cross}`; `target` materializes via `CREATE OR REPLACE TABLE` and registers. |
| **`engine.pivot(name, *, on, using, aggregate="sum", group_by=None, target=None)`** / **`engine.unpivot(name, *, columns, name_column="variable", value_column="value", target=None)`** | Wrap DuckDB's `PIVOT` / `UNPIVOT` statements. Pivot accepts `aggregate ∈ {sum, avg, min, max, count, first}`. Unpivot melts the listed columns into long form. |
| **`engine.aggregate(name, *, aggregates, group_by=None, where=None, having=None, order_by=None, limit=None, target=None)`** | Composed `GROUP BY` with typed validation. `aggregates` is a list of `(func, column[, alias])` tuples; `func` is one of `sum / avg / min / max / count / count_distinct / median / stddev / variance / first / last`. The table, every column, and every aggregate function are validated up-front with did-you-mean suggestions on a miss; `where` and `having` remain opaque DuckDB SQL fragments (predicate shapes are unbounded). |
| **`engine.filter(name, *, where, limit=None, target=None)`** | Typed `SELECT * WHERE`. The table is validated; `where` is an opaque DuckDB SQL predicate. Useful when you want a structured-event audit trail (the call shows up in `engine.history()` as `filter:<table>`) instead of an opaque `query`. |
| **`engine.top_k(name, *, by, n=10, ascending=False, target=None)`** | `ORDER BY ... LIMIT N` over one or more columns; defaults to descending so "top N by units" reads naturally. Set `ascending=True` for bottom-N. |
| **`engine.save(path)` / `engine.to_tabular_document(name)`** | Persist the full database via DuckDB `EXPORT DATABASE`, or convert one registered table back into a `kaos-content` `TabularDocument` (with full row count, even when DuckDB's row stream was truncated). |
| **Did-you-mean error messages** | Every error mentioning a missing table, column, or aggregate function carries a `Did you mean '<closest match>'?` suggestion (via `difflib.get_close_matches` with a 0.6 cutoff). Wired into `describe_table`, `sample`, `count`, `find_duplicates`, `correlation`, `join`, `pivot`, `unpivot`, `aggregate`, `filter`, `top_k`, `export_table` — agents fix typos without an extra `describe` round-trip. |
| **Errors (`KaosTabularError`, `EngineError`, `QueryError`, `RegistrationError`)** | Dedicated exception hierarchy. The MCP layer translates these into `ToolResult.create_error()` with the documented three-part recovery hint (what / how to fix / alternative tool). |
| **`EngineEvent`** + **`engine.history(*, last_n=20)`** | Frozen dataclass `(timestamp, event_type, detail, table_names)` and method returning the recent event log (registers, queries, drops). Provenance trail for an MCP session. |
| **The 17 MCP tools** | Core 8 — `kaos-tabular-{register, query, list-tables, describe, sample, count, read-file, export}` — plus the 6 reshape additions: `kaos-tabular-{history, find-duplicates, correlation, join, pivot, unpivot}` — plus the 3 structured shape tools introduced alongside did-you-mean errors: `kaos-tabular-{aggregate, filter, top-k}`. Registration paths and pure metadata reads are closed-world; arbitrary-SQL `query`, file-reading `register` / `read-file`, and the destructive `export` are open-world; all set explicit `ToolAnnotations`. Register with `register_tabular_tools(runtime)`. |
| **Trust model — [`docs/security.md`](docs/security.md)** | DuckDB is in-process; SQL has filesystem access matching the running process. The query tool's description is honest about this; deployments that need stricter isolation should run kaos-tabular in a constrained working directory or container, or use `kaos_content.bridges.duckdb.create_safe_connection` for an `enable_external_access=false` connection (which cannot register files). |

## CLI

`kaos-tabular` ships two console scripts. Every structured subcommand on
the admin CLI accepts `--json` for machine-readable output piped to
other agents:

```bash
kaos-tabular --help                                       # admin CLI
kaos-tabular-serve --help                                 # MCP server

kaos-tabular query orders.csv "SELECT region, SUM(amount) FROM orders GROUP BY region"
kaos-tabular describe orders.csv --json                   # schema + sample values
kaos-tabular sample orders.csv --rows 10                  # random rows as markdown
kaos-tabular count orders.csv --table orders              # fast row count
kaos-tabular export orders.csv -o orders.parquet          # COPY → parquet
kaos-tabular read orders.csv --json                       # TabularDocument summary

kaos-tabular-serve                                        # stdio (Claude Code / Desktop)
kaos-tabular-serve --http --port 8000                     # streamable HTTP
```

`kaos-tabular query` opens a fresh in-memory engine, registers the input
file, and runs the SQL — useful for one-shots without standing up a
server. For `.duckdb` files the engine opens the database directly in
read-only mode. `kaos-tabular-serve` exposes the seventeen MCP tools
listed in **Concepts** above; it requires the `[mcp]` extra.

## Compatibility & status

| Aspect | |
|---|---|
| **Python** | 3.13, 3.14 |
| **OS** | Linux, macOS, Windows (pure Python — `Operating System :: OS Independent`; DuckDB ships native wheels for x86_64 and arm64 on all three) |
| **Maturity** | 0.1.0 GA. The public API is documented in `kaos_tabular.__all__`: `EngineError`, `EngineEvent`, `KaosTabularError`, `QueryError`, `RegistrationError`, `TabularEngine`, `read_csv`, `read_json`, `read_parquet`, `__version__`. |
| **Stability policy** | Pre-1.0: minor bumps may change behaviour. Every change is documented in [`CHANGELOG.md`](CHANGELOG.md). The MCP tool surface (`kaos-tabular-*` names) and the trust contract documented in [`docs/security.md`](docs/security.md) are public API and follow the same policy. After 1.0 we follow semver. |
| **Test coverage** | 283 unit tests (`tests/unit/`) covering the engine, registration paths, error hierarchy, did-you-mean suggestions, MCP tools, CLI, and `serve.py`; a 32-test integration suite (`tests/integration/`) exercising real DuckDB sessions; and a relocated benchmark suite under `tests/benchmarks/` (6 benchmarks) for wall-clock regressions. Bounded unit gate: `pytest tests/unit -m "not benchmark"`. Coverage floor enforced via `fail_under = 70` in `[tool.coverage.report]`. |
| **Type checker** | Validated with [`ty`](https://docs.astral.sh/ty/), Astral's Python type checker. |

## Documentation

Per-package reference: [`docs/`](docs/) in this repo.

Cross-cutting KAOS guides (agentic patterns, persona presets, settings
policy, citations, MCP data flow, migration to 0.1.0 GA) live in
[`kaos-modules/docs/guides/`](https://github.com/273v/kaos-modules/tree/main/docs/guides).

## Companion packages

`kaos-tabular` is one of the packages in the
[Kelvin Agentic OS](https://kelvin.legal). The broader stack:

| Package | Layer | What it does |
|---|---|---|
| [`kaos-core`](https://github.com/273v/kaos-core) | Core | Foundational runtime, MCP-native types, registries, execution engine, VFS |
| [`kaos-content`](https://github.com/273v/kaos-content) | Core | Typed document AST: Block/Inline, provenance, views |
| [`kaos-mcp`](https://github.com/273v/kaos-mcp) | Bridge | FastMCP server, `kaos` management CLI, MCP resource templates |
| [`kaos-pdf`](https://github.com/273v/kaos-pdf) | Extraction | PDF → AST with provenance |
| [`kaos-web`](https://github.com/273v/kaos-web) | Extraction | Web extraction, browser automation, search, domain intelligence |
| [`kaos-office`](https://github.com/273v/kaos-office) | Extraction | DOCX / PPTX / XLSX readers + writers to AST |
| [**`kaos-tabular`**](https://github.com/273v/kaos-tabular) | **Extraction** | **DuckDB-powered SQL analytics** |
| [`kaos-source`](https://github.com/273v/kaos-source) | Data | Government + financial data connectors (Federal Register, eCFR, EDGAR, GovInfo, PACER, GLEIF) |
| [`kaos-llm-client`](https://github.com/273v/kaos-llm-client) | LLM | Multi-provider LLM transport |
| [`kaos-llm-core`](https://github.com/273v/kaos-llm-core) | LLM | Typed LLM programming (Signatures, Programs, Optimizers) |
| [`kaos-nlp-core`](https://github.com/273v/kaos-nlp-core) | Primitives (Rust) | High-performance NLP primitives |
| [`kaos-nlp-transformers`](https://github.com/273v/kaos-nlp-transformers) | ML | Dense embeddings + retrieval |
| [`kaos-graph`](https://github.com/273v/kaos-graph) | Primitives (Rust) | Graph algorithms + RDF/SPARQL |
| [`kaos-ml-core`](https://github.com/273v/kaos-ml-core) | Primitives (Rust) | Classical ML on the document AST |
| [`kaos-citations`](https://github.com/273v/kaos-citations) | Legal | Legal citation extraction, resolution, verification |
| [`kaos-agents`](https://github.com/273v/kaos-agents) | Agentic | Agent runtime, memory, recipes |
| [`kaos-reference`](https://github.com/273v/kaos-reference) | Sample | Reference module for module authors |

Packages depend on `kaos-core`; everything else is opt-in. Mix and match the
ones you need.

## Development

```bash
git clone https://github.com/273v/kaos-tabular
cd kaos-tabular
uv sync --group dev
```

Install pre-commit hooks (recommended — they run the same checks as CI on
every commit, scoped to staged files):

```bash
uvx pre-commit install
uvx pre-commit run --all-files     # one-time full sweep
```

Manual QA commands (the same set CI runs):

```bash
uv run ruff format --check kaos_tabular tests
uv run ruff check kaos_tabular tests
uv run ty check --exclude kaos_tabular/serve.py kaos_tabular tests
uv run pytest tests/unit -m "not benchmark"          # bounded unit gate
uv run pytest tests/benchmarks                       # perf regression suite
```

## Build from source

```bash
uv build
uv pip install dist/*.whl
python -c "import kaos_tabular; print(kaos_tabular.__version__)"  # smoke import
```

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for setup, quality gates, pull request expectations, and engineering
standards. By contributing you agree to follow the
[project conduct expectations](CODE_OF_CONDUCT.md) and certify the
[Developer Certificate of Origin v1.1](https://developercertificate.org/) —
sign every commit with `git commit -s`. Please open an issue before starting
on a non-trivial change so we can align on scope.

## Security

For security issues, **please do not file a public issue**. Report privately
via [GitHub Private Vulnerability Reporting](https://github.com/273v/kaos-tabular/security/advisories/new)
or email **security@273ventures.com**. See [SECURITY.md](SECURITY.md) for the
full disclosure policy.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 [273 Ventures LLC](https://273ventures.com).
Built for [kelvin.legal](https://kelvin.legal).
