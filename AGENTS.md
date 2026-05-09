# Repository Agent Guidance

## Scope

This file is the canonical instruction file for coding agents working in
this repository. Follow it together with [CONTRIBUTING.md](CONTRIBUTING.md)
and the standards under [docs/standards/](docs/standards/). When guidance
conflicts, prefer the more specific repository document and ask for
maintainer direction before making a broad change.

Keep changes focused. Do not mix behavior changes, formatting churn,
generated files, dependency updates, and release work unless the task
explicitly requires it.

## Project Identity

- Distribution name: `kaos-tabular`.
- Import package: `kaos_tabular`.
- Runtime: Python 3.13+.
- Core purpose: a DuckDB-backed tabular registration, query, inspection,
  and export layer with typed `kaos-content` table/document results.
- Public surfaces include the Python API, CLI entry points
  `kaos-tabular` and `kaos-tabular-serve`, MCP tool names and schemas,
  documented JSON output, errors, and package metadata.

## Setup

Use `uv` for environments, dependency resolution, builds, and local
commands:

```bash
uv sync --group dev
```

Install pre-commit hooks when doing ongoing work:

```bash
uvx pre-commit install
```

The optional MCP server path is behind the `mcp` extra and the `dev-mcp`
dependency group. Keep optional MCP imports lazy and do not make the base
install depend on MCP-only packages.

## Local Checks

Use the quality gate from [CONTRIBUTING.md](CONTRIBUTING.md) for ordinary
changes:

```bash
uv run ruff format --check kaos_tabular tests
uv run ruff check kaos_tabular tests
uv run ty check --exclude kaos_tabular/serve.py kaos_tabular tests
uv run pytest tests/unit -m "not benchmark" --no-cov
```

This repository uses `ruff` for formatting and linting, `ty` for type
checking, and `pytest` for tests. Do not substitute mypy for `ty`; inline
typing suppressions use `# ty: ignore[...]`.

Run packaging checks only when packaging, release metadata, README
rendering, or build behavior changes:

```bash
uv build
uvx --from twine twine check --strict dist/*
```

## Architecture Rules

Follow [Python design and architecture](docs/standards/python-design-and-architecture.md)
and keep the package native to this repository.

- Preserve `TabularEngine` as a thin, typed owner of a session-scoped
  DuckDB connection. DuckDB execution should continue to flow through
  the established `kaos_content.bridges.duckdb` boundary.
- Preserve typed result and schema contracts. Prefer explicit dataclasses,
  Pydantic models at external boundaries, and `kaos-content` `Table` /
  `TabularDocument` results over raw cursors or loose dictionaries.
- Keep registration/query/export behavior coherent for CSV, TSV, JSON,
  JSONL, Parquet, SQLite, and the documented XLSX-as-`Table` workflow.
  Do not add direct XLSX parsing to the base package without an explicit
  dependency and documentation decision.
- Keep SQL safety behavior honest and bounded: quote identifiers and
  literals through structured helpers, validate typed operation inputs,
  preserve row/resource caps, and keep errors actionable without leaking
  secrets or irrelevant internals.
- Keep CLI and MCP behavior stable. Tool names, schemas, annotations,
  JSON shapes, exit behavior, and recovery-oriented error messages are
  user-facing contracts.
- Keep optional dependency boundaries clean. The base package remains
  small; MCP server support belongs behind the declared optional extra.

## Testing

Follow [Tests, fixtures, and CI](docs/standards/tests-fixtures-ci.md).

- Add regression tests for bug fixes and tests through real public entry
  points for new public behavior.
- Keep unit tests deterministic, offline, and credential-free.
- Use redistributable fixtures only. Fixture and golden-output changes
  must be small, intentional, and reviewable.
- For tabular behavior, cover accepted and rejected cases around SQL
  quoting, path handling, schema contracts, row limits, file-format
  handling, CLI JSON output, and MCP tool responses.
- Benchmarks live under `tests/benchmarks/`; update or run them when a
  change can affect material query, registration, or export performance.

## Security

Follow the repository security policy and `docs/security.md`.

- Never commit secrets, credentials, private keys, `.env` files, customer
  data, or unknown-license fixtures.
- Treat SQL, file paths, table names, column names, output paths, and
  MCP inputs as untrusted unless the calling surface states otherwise.
- DuckDB runs in process and has the filesystem access of the running
  process. Do not imply stronger isolation than the package actually
  enforces.
- Preserve bounded query behavior and explicit trust-contract wording
  for arbitrary SQL, file registration, and export.

## Commits, PRs, And Releases

Follow [Engineering process](docs/standards/engineering-process.md) and
[Code quality standards](docs/standards/code-quality-standards.md).

- Use conventional commit style and sign commits with `git commit -s`.
- Rebase topic branches on `main` before review when needed. Do not
  force-push unless a maintainer explicitly directs it for that branch.
- PRs should state what changed, why, how it was tested, and whether
  public API, CLI behavior, MCP behavior, package metadata, fixtures,
  security behavior, or release artifacts changed.
- User-visible changes need documentation updates and a `CHANGELOG.md`
  entry under `[Unreleased]`.
- Release work must preserve immutable tags, strict build metadata
  checks, fresh install smoke tests, and the documented publishing flow.
