# Security policy

## Reporting a vulnerability

We take security seriously. If you believe you have found a security
vulnerability in `kaos-tabular`, please report it privately so we can address it
before public disclosure.

**Please do not file a public GitHub issue for security reports.**

### How to report

Use [GitHub Private Vulnerability Reporting](https://github.com/273v/kaos-tabular/security/advisories/new)
to send a report. Alternatively, email **security@273ventures.com**.

Include as much of the following as you can:

- A description of the vulnerability and its impact
- Steps to reproduce, including affected versions
- Any proof-of-concept code, if available
- Suggested mitigations, if you have any

### What to expect

- **Acknowledgement** — within 3 business days of your report.
- **Initial triage** — within 7 business days, including a severity assessment.
- **Fix and disclosure** — coordinated with you. Our target window is 90 days
  from acknowledgement to public disclosure, faster for high-severity issues.
- **Credit** — we credit reporters in the release notes and security advisory
  unless you prefer to remain anonymous.

## Supported versions

`kaos-tabular` follows Semantic Versioning. While the project is pre-1.0, only
the latest minor release receives security fixes. After 1.0, the latest two
minor releases will be supported.

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
| < 0.1   | No        |

## Scope

The trust contract `kaos-tabular` enforces is documented in
[`docs/security.md`](docs/security.md). DuckDB is in-process; SQL has
filesystem access scoped to the running process; deployments wanting
stricter isolation should run kaos-tabular in a constrained working
directory or container. Reports against the documented contract are
in scope; reports complaining that DuckDB CAN read files the process
can read are not (it is documented behaviour).

In-scope:

- The `kaos-tabular` Python package as published on PyPI.
- The `273v/kaos-tabular` GitHub repository — CI configuration,
  release workflow, supply-chain artifacts (Sigstore attestations,
  wheel + sdist contents, NOTICE attribution).
- **DuckDB SQL execution boundary** — anything that lets SQL escape
  the documented trust contract: parameter / identifier / string-literal
  injection (e.g. `_register_sqlite`, `export_table`, `save`), bypass
  of the `LIMIT N` wrapper that blocks DROP / multi-statement on
  user-supplied SQL, evasion of the 10 000-row hard cap.
- **File registration paths** — `register_file`, `register_table`,
  `register_document`. Adversarial CSV / Parquet / JSON / SQLite
  inputs that crash the engine, exfiltrate filesystem content beyond
  what the trust contract allows, or escalate the engine's
  filesystem access.
- **Export / write paths** — `export_table` and `save`. Path
  injection via embedded quotes in the caller-supplied path is the
  primary concern; `_q_lit` is the load-bearing mitigation.
- **MCP tool surface** — the 8 `kaos-tabular-*` tools. Tool
  annotations (`openWorldHint`, `destructiveHint`) drive client
  auto-approval decisions; mismatches between annotation and actual
  behaviour are in-scope.
- **SQLite extension fetch** — `register_file` of a `.sqlite` file
  triggers DuckDB's `INSTALL sqlite` / `LOAD sqlite`, which performs
  a network fetch from `extensions.duckdb.org` on first use unless
  the extension is pre-bundled. Compromise of that channel is in
  scope insofar as we route through it.
- **Transitive dependency supply chain** — anything in
  `Requires-Dist` of the published wheel. Coordinate the report with
  the upstream project before public disclosure where possible.

Out of scope:

- DuckDB engine bugs themselves — report to
  [duckdb/duckdb](https://github.com/duckdb/duckdb/security). We
  escape user input *around* DuckDB; we don't ship DuckDB internals.
- Third-party dependency CVEs that don't affect a kaos-tabular
  surface — report to the upstream project (kaos-content, kaos-core,
  Apache Arrow, etc.).
- Issues caused by user-supplied configuration that explicitly opts
  out of the trust contract — for example, loading the DuckDB
  `httpfs` extension and then handing the engine SQL from an
  untrusted source, or running `kaos-tabular-serve` with no
  filesystem isolation against an untrusted MCP client.
- The XLSX migration path documented in `0.1.0a1`'s CHANGELOG —
  parsing happens in `kaos-office`, not here. Report XLSX-parser
  issues to that package.
