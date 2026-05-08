"""Path-injection coverage for save() + export_table() — post-release-review #2.

DuckDB's ``EXPORT DATABASE`` and ``COPY ... TO`` accept a target path
as a SQL string literal; there is no parameter binding for these
targets. ``_q_lit`` is the load-bearing mitigation. These tests
hand each method a path string with an embedded single quote +
injected SQL, register a marker table to look for, and assert the
injection did NOT fire (no ``pwned`` table appears in the engine).

Pre-fix (post-release-review #2), ``save()`` interpolated ``p``
directly into ``EXPORT DATABASE '{p}'`` without escaping; a path
like ``/tmp/x'; CREATE TABLE pwned AS SELECT 1; --`` broke out of
the literal and ran the injected statement. The same hazard
applied to ``export_table()`` until commit ``e7bd54d`` switched it
to share ``_q_lit``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from kaos_tabular.engine import TabularEngine


def _seed_simple_table(engine: TabularEngine, tmp_path: Path) -> str:
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
    return engine.register_file(csv_path, table_name="seed")


def test_save_path_injection_does_not_fire(tmp_path: Path) -> None:
    """Hostile path with embedded quote + injected ``CREATE TABLE pwned``."""
    with TabularEngine() as engine:
        _seed_simple_table(engine, tmp_path)

        # Build the hostile target. ``mkdir`` runs first against the
        # raw path, so we choose one that's also a valid directory
        # name on Linux: parent dir is real, the name is just a
        # quote-bearing string.
        host_dir = tmp_path / "evil_save_dir"
        host_dir.mkdir()
        hostile_target = f"{host_dir}/x'; CREATE TABLE pwned AS SELECT 1 AS x; --"

        # save() may legitimately fail when the underlying path is
        # malformed for ``EXPORT DATABASE`` — that is acceptable. The
        # contract under test is: the injected statement does not run.
        with contextlib.suppress(Exception):
            engine.save(hostile_target)

        names = {t["name"] for t in engine.list_tables()}
        assert "pwned" not in names, (
            f"save() path injection succeeded: 'pwned' appeared. Tables: {sorted(names)}"
        )


def test_export_table_path_injection_does_not_fire(tmp_path: Path) -> None:
    """Hostile output_path on ``export_table`` must stay inside the literal."""
    with TabularEngine() as engine:
        _seed_simple_table(engine, tmp_path)

        host_dir = tmp_path / "evil_export_dir"
        host_dir.mkdir()
        hostile_target = f"{host_dir}/out.parquet'; CREATE TABLE pwned AS SELECT 1; --"

        with contextlib.suppress(Exception):
            engine.export_table("seed", hostile_target, format="parquet")

        names = {t["name"] for t in engine.list_tables()}
        assert "pwned" not in names, (
            f"export_table() path injection succeeded: 'pwned' appeared. Tables: {sorted(names)}"
        )
