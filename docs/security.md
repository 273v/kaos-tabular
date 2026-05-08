# kaos-tabular Trust Model

This module exposes a DuckDB-backed SQL engine to MCP clients (and to
direct Python callers). DuckDB is an **in-process** database — its
filesystem access is exactly the running Python process's filesystem
access. There is no network or remote SQL involved.

The audit (audit-01 KTAB-001) called out a gap between the prior
`kaos-tabular-query` description ("queries against registered tables")
and what the tool actually accepts (any DuckDB SQL, including
`read_csv_auto('...')` and friends that read files outside the
registered set). This document is the canonical statement of the
boundary kaos-tabular enforces and the boundary it leaves to the
deployment.

## What `kaos-tabular-query` accepts

- Any DuckDB-parseable SQL statement.
- Multi-statement input is rejected by DuckDB's parser. The tool
  additionally wraps the user's SQL as `SELECT * FROM (<sql>) AS _q
  LIMIT N`, so `DROP TABLE`, `CREATE TABLE`, etc. submitted via the
  query tool fail at parse time. This is a defence in depth, not the
  primary boundary.
- Filesystem access via DuckDB built-ins (`read_csv_auto`,
  `read_parquet`, `read_json_auto`, `read_ndjson`, `glob`, `COPY`,
  `EXPORT DATABASE`, etc.) is **available**. It is what makes
  `kaos-tabular-register` work — we cannot disable it without breaking
  the registration API.

## What it does NOT enforce

- File-system isolation. A SQL caller can read any path the Python
  process can read.
- Network isolation. DuckDB's `httpfs` extension would, if installed,
  let SQL fetch HTTP/S URLs. We do not install or load it; if your
  deployment loads it, the same property applies — SQL gets the
  process's network reach.
- Filesystem write isolation. `COPY ... TO '<path>'` writes wherever
  the process can write. This is intentional — `TabularEngine.export_table()`
  uses exactly this to implement the `export` MCP tool.

## What deployments should do

If you run kaos-tabular against untrusted SQL (e.g. from an LLM agent)
and the surrounding process can read or write files you don't want
exposed:

1. **Run kaos-tabular in a constrained working directory.** Start the
   server with `cwd` set to a directory that only contains the data
   you want callers to register against.
2. **Use a container or unprivileged user.** A Docker container with a
   read-only volume for the data directory is the simplest robust
   answer.
3. **Pre-register the tables you want exposed and disable
   `kaos-tabular-register`.** Strip `RegisterTool` from the runtime
   if registration should not be agent-driven; the query tool will
   still run, but only against the tables you pre-loaded.

## The strict-isolation alternative

When you do not need to register files at all and only want to run SQL
against `TabularDocument` instances handed in via Python, use
[`kaos_content.bridges.duckdb.create_safe_connection()`](https://github.com/273v/kaos-content)
directly. That factory issues `SET enable_external_access = false` +
`SET lock_configuration = true`, blocking all filesystem and network
access at the engine level for the connection's lifetime. This
connection cannot back a `TabularEngine` (because every `register_file()`
path uses external readers), but it is the right choice for callers
that only register Python-resident `TabularDocument` objects.

## Quick reference

| Surface | Trust contract |
|---|---|
| Direct Python `TabularEngine.execute(sql)` | Caller is the trust boundary. |
| `kaos-tabular-query` (MCP) | Tool description is now explicit; deployment must isolate the running process. |
| `TabularEngine.register_file(path)` | Path must exist and be process-readable. |
| `TabularEngine.export_table(name, path, format)` | Path must be process-writable. SQL identifier and target path are escaped. |
| `kaos_content.bridges.duckdb.create_safe_connection` | Strict isolation; cannot register files. |

## History

- **2026-05-08** — Initial statement of the trust model, alongside the
  audit-01 KTAB-001 fix (description honesty + this document). No code
  change was required to the engine's filesystem behaviour; the gap was
  in how the contract was advertised, not in what it enforced.
