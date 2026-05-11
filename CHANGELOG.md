# Changelog

All notable changes to `kaos-tabular` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Documented the SQL-quoting safety contract on six query sites in
  ``engine.py`` (bandit B608).** ``TabularEngine`` builds SQL via
  f-strings against table/column/path inputs and routes every dynamic
  fragment through one of two validating quoters:
  ``_quote_ident`` (validates + double-quotes the identifier) or
  ``_q_lit`` (doubles single quotes for SQL string literals). Bandit's
  static B608 heuristic can't see the quoter — it just sees an
  f-string concatenating SQL fragments — so every call site is
  flagged as a possible SQL-injection vector. Added inline
  ``# nosec B608`` comments at each site with a one-line justification
  pointing at the relevant quoter; the quoting contract itself is
  unchanged. Files: ``kaos_tabular/engine.py``.
- **bandit + vulture now run in both pre-commit and CI.** Two new
  hooks in ``.pre-commit-config.yaml`` (bandit + vulture), mirrored
  by two new jobs in ``security.yml`` (``bandit (static security)``
  + ``vulture (dead-code scan)``). Pre-commit gives contributors fast
  feedback before push; CI makes the scan publicly visible on every
  PR. Skip lists justified inline. Mirrors the rollout from
  kaos-core. **Depends on PR #1** (bandit B608 nosec justifications
  in engine.py) — bandit will fail on this branch's first run until
  #1 merges, then rebase clears it.
### Changed

- **uv.lock bumped to the current PyPI-latest of three kaos-* siblings:**
  ``kaos-content`` 0.1.0a2 → 0.1.0a4, ``kaos-core`` 0.1.0a4 →
  0.1.0a5, and ``kaos-mcp`` 0.1.0a1 → 0.1.0a2. All three bumps are
  no-op for kaos-tabular's public API. 276 unit tests continue to
  pass.

## [0.1.0a1] — 2026-05-08

### Added (structured shape tools + did-you-mean error suggestions)

A second pre-tag pass reconsidered the "tools earn their weight when
SQL is genuinely awkward" framing. The framing held for `pivot`,
`unpivot`, `join`, and `correlation`, but was too narrow for the
`GROUP BY` / `WHERE` / `ORDER BY ... LIMIT` trio: agents write that
SQL correctly, yes, but typed wrappers buy validation at the boundary,
structured-event audit (the call shows up in `engine.history()` as
`aggregate:<table>` instead of an opaque `query:` string), and
dialect-insulation if the engine ever grows a non-DuckDB backend.

Three new MCP tools (14 → 17) and matching public engine methods:

- **`kaos-tabular-aggregate`** + **`engine.aggregate(table, *, aggregates,
  group_by=None, where=None, having=None, order_by=None, limit=None,
  target=None)`**. Composed `GROUP BY`. `aggregates` is a list of
  `(func, column[, alias])` tuples; `func` ∈ `{sum, avg, min, max,
  count, count_distinct, median, stddev, variance, first, last}`.
  Validates the table, every column, and every aggregate function
  *before* SQL is generated, with did-you-mean suggestions on a miss.
  `where` / `having` remain opaque DuckDB SQL fragments (predicate
  shapes are unbounded). `order_by` items must reference either a
  group_by column or an explicit aggregate alias; bare aggregate
  expressions in `ORDER BY` are rejected at the wrapper.
- **`kaos-tabular-filter`** + **`engine.filter(table, *, where,
  limit=None, target=None)`**. Typed `SELECT * WHERE`. The table is
  validated; `where` is opaque DuckDB SQL. Useful when the caller
  wants the call to show up in the structured history log under
  `filter:<table>` instead of inside an opaque `query:` event.
- **`kaos-tabular-top-k`** + **`engine.top_k(table, *, by, n=10,
  ascending=False, target=None)`**. `ORDER BY ... LIMIT N`. Defaults
  to descending so "top N by units" reads naturally; pass
  `ascending=True` for bottom-N.

### Added (did-you-mean suggestions across the engine)

Every error path that mentions a missing table or column now carries
a `Did you mean '<closest match>'?` suggestion using
`difflib.get_close_matches` with a 0.6 cutoff. The cutoff is high
enough to avoid spurious matches on short identifiers (`id` / `ip`)
but low enough to forgive single-character typos on typical 6+
character column names.

The mechanism is wired into `describe_table`, `sample`, `count`,
`find_duplicates`, `correlation`, `join` (both sides + `on=`),
`pivot`, `unpivot`, `export_table`, and the three new structured
shape methods (`aggregate`, `filter`, `top_k`). The aggregate
function whitelist also gets did-you-mean against the supported
function names. Module-level `_suggestions` and
`_did_you_mean_fragment` helpers are unit-tested in isolation against
the cutoff edge-cases (empty universe, no near-match, plural form);
the `TestExistingErrorPathsRetrofit` class pins the retrofit so a
future refactor can't silently drop suggestions from the older
analytical surfaces.

Test count: 216 → 276 unit tests (60 new in
`tests/unit/test_structured_ops.py`); coverage stays above the 70%
`fail_under` floor.

Quick benchmark (100k-row CSV, 5 distinct group keys): structured
`aggregate` runs 7.5 ms median vs. 3.4 ms for the equivalent raw
`execute` — ~4 ms validation overhead from two `information_schema`
lookups per call. The overhead is constant regardless of data size,
acceptable for interactive agent use; throughput-bound batch loops
should reach for `kaos-tabular-query` instead.

### Fixed (post-release-review pass before tag)

External review found gaps the audit-01 sweep missed; all addressed
before tagging:

- **#1 P0: SQLite table-name SQL injection in `_register_sqlite`.**
  `src_table` values from `sqlite_master` were interpolated raw into
  the next `sqlite_scan('{path}', '{src_table}')` call. A crafted
  SQLite file with a hostile table name could escape the literal and
  execute injected DuckDB SQL. New module-level `_q_lit` helper
  performs the standard `'` → `''` escape; both the path and the
  `src_table` now flow through it. Adversarial test in
  `tests/unit/test_sqlite_register.py::test_register_sqlite_hostile_table_name_does_not_inject`.
- **#2 P0: `save()` path SQL injection.** `EXPORT DATABASE '{p}'`
  pasted the caller-supplied path directly. `save("'; ATTACH ...; --")`
  could break out of the literal. Same `_q_lit` mitigation.
  `export_table` (added in this release) was already correct but is
  now consolidated onto `_q_lit` for consistency. Adversarial tests
  in `tests/unit/test_path_injection.py`.
- **#3 P0: `duckdb` minimum lifted from `>=1.0` to `>=1.4.2`.** 1.0.0
  has no cp313 wheel; 1.1.1 was the first cp313 release; 1.4.2 was
  the first cp314 release. Since we support both 3.13 and 3.14, the
  floor must clear both — pre-1.4.2 made the lowest-direct CI job
  build duckdb from source on cp314, which is why min-deps took
  20+ minutes.
- **#4 P0: MCP tool annotations now match real behaviour.** Pre-fix,
  every tool used `_TABULAR_ANNOTATIONS` with `openWorldHint=False`,
  including ones that genuinely reach the filesystem
  (`Register` / `Query` / `ReadFile`); `ExportTool` used
  `_TABULAR_WRITE_ANNOTATIONS` with `destructiveHint=False` despite
  writing/overwriting files. Split into three classes:
  `_TABULAR_READ_ANNOTATIONS` (closed-world catalog reads — `List` /
  `Describe` / `Sample` / `Count`), `_TABULAR_OPEN_READ_ANNOTATIONS`
  (open-world filesystem reads — `Register` / `Query` / `ReadFile`),
  `_TABULAR_WRITE_ANNOTATIONS` (open-world destructive writes —
  `Export`, now `destructiveHint=True`). Agents make auto-approval
  decisions on these flags; getting them right is the largest
  actual safety improvement in this commit.
- **#5 P1: `_ENGINES` cache bounded with LRU + close-on-evict.**
  Pre-fix, the per-session engine cache was an unbounded `dict`;
  long-running streamable-HTTP servers leaked DuckDB connections
  forever. Now an `OrderedDict` capped at
  `_ENGINES_MAX_SESSIONS = 64`; the oldest engine is closed on
  insert past capacity. TODO: replace with proper kaos-mcp
  per-session lifecycle hook at 0.1.0a2. Coverage in
  `tests/unit/test_session_engines.py`.
- **#6 P1: stale integration assertion fixed.**
  `tests/integration/test_mcp_tabular_pipeline.py` asserted the
  pre-KTAB-007 error string `"Cannot infer format"`. Updated to the
  current `"Cannot infer export format"`. CI doesn't gate the
  integration tier today; raised as a separate platform tracker.
- **#7 P1: SECURITY.md scope rewritten for kaos-tabular.** The
  template carried over from kaos-mcp listed LLM/program-execution/
  cache/provider concerns that don't apply here. New scope names:
  the DuckDB SQL boundary, file registration paths, export/write
  paths, MCP tool surface, the SQLite extension network fetch, the
  transitive dep supply chain.

### Added (post-Kelvin-comparison surface expansion)

A pre-tag review against the legacy ``kelvin_tabular`` package
(roughly 60 MCP tools across inspection / manipulation / statistics /
quality / transformation categories) found that most of those tools
were SELECT one-liners that don't earn their weight when the agent
already has free-form SQL. The ones that *do* earn their weight are
the SQL-is-genuinely-awkward cases — joins where column ambiguity
catches agents writing `JOIN ON l.x = r.x` by hand, the
``PIVOT`` / ``UNPIVOT`` syntax, long-form correlation matrices,
provenance tracing — and those are the six we ported. The package
explicitly does NOT ship Kelvin's full tree; SQL is the expression
layer for everything else.

Six new MCP tools (8 → 14) and matching public engine methods:

- **``kaos-tabular-history``** + **``engine.history(*, last_n=20)``**
  + ``EngineEvent`` exported on the public surface. Returns the
  recent register / query / drop events for the session — provenance
  for agents tracing back what's been loaded.
- **``kaos-tabular-find-duplicates``** + **``engine.find_duplicates(table, *, columns=None)``**.
  Returns rows that share their key with at least one other row,
  via DuckDB ``QUALIFY COUNT(*) OVER (PARTITION BY …) > 1``. Default
  ``columns=None`` uses every column (full-row duplicate detection).
- **``kaos-tabular-correlation``** + **``engine.correlation(table, *, columns=None)``**.
  Pairwise Pearson correlation between numeric columns, returned as
  long-form ``(col_a, col_b, corr)`` rows. Default auto-selects
  every numeric column from the catalog.
- **``kaos-tabular-join``** + **``engine.join(left, right, *, on, how="inner", target=None)``**.
  Wraps DuckDB's ``USING (col)`` clause so the join key appears
  once in the result. ``how`` ∈ ``{inner, left, right, outer, semi,
  anti, cross}``; ``target`` materializes via
  ``CREATE OR REPLACE TABLE`` and registers.
- **``kaos-tabular-pivot``** + **``engine.pivot(table, *, on, using,
  aggregate="sum", group_by=None, target=None)``**. Wraps DuckDB
  ``PIVOT``. ``aggregate`` ∈ ``{sum, avg, min, max, count, first}``.
- **``kaos-tabular-unpivot``** + **``engine.unpivot(table, *, columns,
  name_column="variable", value_column="value", target=None)``**.
  Wraps DuckDB ``UNPIVOT``.

Each tool declares its own per-tool ``ToolAnnotations`` literal
(closed-world for catalog-only ops, open-world for arbitrary SQL,
destructive-write for ``export``). Engine methods emit 3-part
errors via ``EngineError`` and the MCP layer forwards them through
``ToolResult.create_error``. New unit-test file
``tests/unit/test_analytical_methods.py`` covers all five engine
methods + their tool wrappers — 27 tests, including round-trips
(pivot then unpivot), edge cases (empty columns list, missing
column, invalid ``how``), and tool-side error translation.

Test count: 189 → 216 unit tests; coverage stays at ~75% above
the 70% ``fail_under`` floor.

### Refactored (post-review code-quality pass)

A self-review against `docs/python/{boundaries,modules,errors,
dry-abstraction}.md` flagged five items worth addressing before tag.
All landed; none change the public API:

- **Item 3: `_ENGINES` global → `EngineRegistry` class.** New
  module `kaos_tabular/_session.py` owning the bounded LRU.
  `EngineRegistry(max_sessions=..., engine_factory=...)` lets tests
  build isolated registries and inject a `_CountingEngine` factory
  to spy on `close()` without monkey-patching module state. The
  process singleton `SESSION_REGISTRY` keeps live MCP-session
  behaviour identical. `tools._get_engine` is now a thin async
  wrapper that delegates to the registry (with the same
  `context is None` ephemeral-engine policy).
- **Item 4: `cast(Literal[...], fmt)` → typed inference helpers.**
  New `_coerce_export_format(value: Any) -> ExportFormat | None`
  and `_infer_export_format_from_extension(ext: str) -> ExportFormat | None`
  return literal types directly so ty sees the narrow without a
  `cast`. ExportTool's `execute` gets simpler too.
- **Item 5: brittle eviction test → `_CountingEngine` subclass.**
  Replaced the `engine.close = lambda: ...` monkey-patch with a
  real `TabularEngine` subclass that bumps a counter. Bonus:
  asserts the evicted engine's DuckDB connection actually raises
  `duckdb.ConnectionException` post-eviction.
- **Item 6: focused `_q_lit` unit tests.** New
  `tests/unit/test_engine_helpers.py` pins six properties + a
  parametrized 7-input round-trip through real DuckDB
  (`SELECT {_q_lit(s)}` → `s`). The adversarial tests still cover
  the engine-end-to-end path; this catches contract drift before it
  reaches them.
- **Item 7: shared annotation constants → per-tool literals.**
  Removed `_TABULAR_READ_ANNOTATIONS` / `_TABULAR_OPEN_READ_ANNOTATIONS`
  / `_TABULAR_WRITE_ANNOTATIONS`. Each of the 8 tools now declares
  its own `ToolAnnotations(...)` literal in its `metadata` property,
  matching the kaos-reference / kaos-citations pattern. Eliminates
  the misclassification-via-shared-constant risk that motivated
  review #4 in the first place.

Tests: 173 → **189** unit tests, 32 integration tests still green,
coverage 75% → 73% (more code under coverage tracking; gate still
above the 70% floor).

### Deferred to next release (tracked, not blocking 0.1.0a1)

- Make `INSTALL sqlite` / `LOAD sqlite` opt-in via a settings flag
  (post-release-review #8). Currently the actionable error path is
  in place (KTAB-010); making the network fetch opt-in is a real
  API change worth doing in a settled release.
- Include `SECURITY.md` in the sdist (post-release-review #9). Cheap
  to do at the cross-package level alongside other sdist policy.
- Pin GitHub Actions and gitleaks Docker image references to SHAs
  for stronger supply-chain posture (post-release-review #10). Best
  done as a platform-wide sweep across all kaos-* repos at once.

## [0.1.0a1-original] — superseded entries below

The remainder of this entry documents the pre-review release
preparation; left intact so the audit-01 / OSS Phase A trail is
preserved.

First public alpha. DuckDB-powered tabular data engine with 8 MCP
tools for register / query / describe / list / sample / count /
export / read-file workflows. Closes every finding in
`docs/audit-01/kaos-tabular.md` (KTAB-001..KTAB-010).

### Removed (dep minimization)

- **`polars` dropped from required dependencies.** A pre-release
  audit confirmed nothing in `kaos_tabular` source or tests imports
  polars; the DuckDB bridge in `kaos-content` doesn't need it
  either (the polars bridge lives behind kaos-content's own
  `[polars]` extra, which kaos-tabular never pulled). Result: the
  resolved tree shrinks 56 → 54 packages and the install no longer
  fetches the polars + polars-runtime-32 native binaries (~30 MB
  combined). The `polars` keyword and the README polars mentions
  are also dropped.

### Compliance

- **License audit (50 distinct deps in the resolved tree).** Every
  inbound license is on the `docs/oss/10-licensing-legal/dep-license-policy.md`
  allowlist: MIT, Apache-2.0, BSD-2/3-Clause, ISC, MPL-2.0 (certifi,
  weak-copyleft permitted), PSF-2.0 (typing-extensions). Zero
  matches against the denylist (GPL family, AGPL family,
  Commons-Clause, SSPL, BUSL, anyone else's proprietary). Audit
  evidence: `uv tree --no-dedupe` × per-PyPI license metadata.

### Added

- **`LICENSE`, `NOTICE`, `CHANGELOG.md`** seeded for the public release.
  License flips from `LicenseRef-Proprietary` to Apache-2.0 via PEP 639
  (`license = "Apache-2.0"`, `license-files = ["LICENSE", "NOTICE"]`).
  `License ::` classifier removed (PEP 639 supersedes).

- **`TabularEngine.export_table(table_name, output_path, format=...)`**
  — public engine method that owns DuckDB COPY, format mapping, and
  path quoting. ExportTool MCP and `kaos-tabular export` CLI now call
  it instead of reaching into `engine._con` and importing the private
  `kaos_content.bridges.duckdb._quote_ident`. Closes audit-01 KTAB-003.

- **`docs/security.md`** — canonical statement of the trust contract
  (DuckDB is in-process; SQL has filesystem access matching the running
  process; deployments wanting stricter isolation should run
  kaos-tabular in a constrained working directory or container; the
  strict-isolation alternative is `kaos_content.bridges.duckdb.create_safe_connection`,
  which cannot register files). Closes audit-01 KTAB-001 alongside the
  description honesty fix.

- **`kaos_tabular/py.typed`** marker so the `Typing :: Typed` classifier
  is honored by downstream type checkers. Closes audit-01 KTAB-004.

- **`benchmark` pytest marker** registered in `pyproject.toml`. Wall-
  clock performance tests relocated from `tests/unit/test_adversarial.py`
  → `tests/benchmarks/test_engine_perf.py`. Bounded unit gates can now
  exclude them with `-m "not benchmark"`. Closes audit-01 KTAB-006.

- **`tests/unit/test_sqlite_register.py`** — positive (real SQLite
  fixture) and negative (forced INSTALL/LOAD failure) coverage for the
  new SQLite registration error path. Closes audit-01 KTAB-010.

- **`tests/unit/test_serve.py`** — argparse + import-error coverage for
  `kaos_tabular.serve.main`, lifting `serve.py` from 0% to ~55% and
  total coverage from 63% (audit baseline) to 73%.

- **`fail_under = 70` coverage gate** in
  `[tool.coverage.report]`. Locks the new floor against regression.
  Closes audit-01 KTAB-005.

### Changed

- **`QueryTool.metadata.description` is now honest** about the trust
  contract: "Execute arbitrary DuckDB SQL against the session's
  in-process engine ... SQL has filesystem access matching the running
  process — for stricter isolation, run kaos-tabular in a constrained
  working directory or container." Previously the description claimed
  "queries against registered tables" while the engine accepted
  arbitrary DuckDB SQL including `read_csv_auto('...')`. Closes
  audit-01 KTAB-001.

- **`_register_sqlite` now raises `RegistrationError` with a 3-part
  message** when DuckDB's `INSTALL sqlite` / `LOAD sqlite` fails. The
  message names the install command, the offline workaround
  (pre-bundled extension), and the fallback (export tables to CSV /
  Parquet first). Closes audit-01 KTAB-010.

- **MCP error messages standardized to the what / how-to-fix /
  alternative-tool shape** across `tools.py`. The audit explicitly
  flagged the sample (`tools.py:359`) and read-file (`tools.py:489`)
  errors as incomplete; both rewritten plus the file-not-found, no-
  tables-registered, and register-failed paths. Closes audit-01
  KTAB-007.

- **Stale comment in `tests/unit/test_tools.py`** removed. The module
  docstring claimed "Several tools have a bug where _get_engine(context)
  is called without await" — current source awaits correctly. Closes
  audit-01 KTAB-009.

### Removed

- **`[xlsx]` extra and `_register_xlsx` method dropped.** Both
  introduced an undocumented sideways
  `kaos-tabular -> kaos-office` extraction-module dependency that the
  architecture DAG explicitly forbids. Callers wanting XLSX support
  parse the file with `kaos_office.parse_xlsx(path)` (in kaos-office,
  which is the right home for OPC reading) and pass each `Table` to
  `engine.register_table(table, name=...)` (already public). The
  workspace dependency on `kaos-office` is removed; `[tool.uv.sources]`
  drops the kaos-office editable entry. Closes audit-01 KTAB-002.

### Notes (audit findings already resolved)

- **KTAB-008** — `kaos_tabular/__init__.py` `__all__` is already
  alphabetically sorted under Python's default ordering (uppercase <
  underscore < lowercase per ASCII). No change needed; documented here
  as verified against `sorted()`.

[Unreleased]: https://github.com/273v/kaos-tabular/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/273v/kaos-tabular/releases/tag/v0.1.0a1
