"""SQLite registration coverage — audit-01 KTAB-010 + post-release-review #1.

Three flavours:

1. Positive: ``register_file`` on a real ``.sqlite`` fixture, on a
   machine that has the DuckDB sqlite extension bundled.
2. Offline-friendly negative: simulate ``INSTALL sqlite`` failing
   (DuckDB raises ``duckdb.IOException``) and assert the error message
   names the install command + the workaround.
3. Adversarial table-name injection (post-release-review #1):
   build a SQLite file whose table name contains a single quote +
   injected SQL, register it, and assert the injected statement did
   NOT fire. ``sqlite_master`` carries data from inside the file —
   an attacker who controls the .sqlite controls those names.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from unittest import mock

import duckdb
import pytest

from kaos_tabular.engine import TabularEngine
from kaos_tabular.errors import RegistrationError


def test_register_sqlite_real_fixture(fixtures_dir: Path) -> None:
    """Positive path: register a real SQLite database file."""
    sqlite_file = fixtures_dir / "ledes98b.sqlite"
    if not sqlite_file.exists():
        pytest.skip("ledes98b.sqlite fixture not vendored")

    with TabularEngine() as engine:
        engine.register_file(sqlite_file)
        tables = engine.list_tables()
        assert tables, "expected at least one SQLite table to register"


def test_register_sqlite_install_failure_raises_actionable_error(
    tmp_path: Path,
) -> None:
    """Negative path: when INSTALL/LOAD sqlite fails (offline / not bundled),
    RegistrationError is raised with an actionable 3-part message.

    DuckDB's connection ``execute`` is a C-level method and can't be
    patched in place; replace the engine's whole ``_con`` with a Mock
    that raises on ``execute("INSTALL sqlite")``.
    """
    sqlite_file = tmp_path / "fake.sqlite"
    sqlite_file.write_bytes(b"")  # contents irrelevant — INSTALL fails first

    engine = TabularEngine()
    try:
        fake_con = mock.MagicMock()
        fake_con.execute.side_effect = duckdb.IOException("Failed to fetch sqlite extension")
        engine._con = fake_con

        with pytest.raises(RegistrationError) as exc_info:
            engine._register_sqlite(sqlite_file, "fake")

        msg = str(exc_info.value)
        assert "sqlite extension" in msg
        assert "duckdb extension install sqlite" in msg
        assert "CSV / Parquet" in msg
    finally:
        engine.close()


def test_register_sqlite_hostile_table_name_does_not_inject(tmp_path: Path) -> None:
    """Adversarial: hostile SQLite table name must not break out of the
    string literal in ``sqlite_scan('{path}', '{src_table}')``.

    The hostile table name is a single quote + injected SQL. Pre-fix
    (post-release-review #1), this name was interpolated raw into the
    DuckDB SQL string literal and the injected ``CREATE TABLE`` fired,
    creating a table called ``pwned`` in the engine. Post-fix, the
    name is escaped via ``_q_lit``, the injection lands inside the
    literal, and ``pwned`` is never created.
    """
    sqlite_file = tmp_path / "hostile.sqlite"

    # Build a SQLite file whose table name carries injection. SQLite
    # accepts arbitrary strings as table names when they're quoted,
    # so this is a realistic attacker payload.
    hostile_name = "evil'); CREATE TABLE pwned AS SELECT 'INJECTED' AS x; --"
    con = sqlite3.connect(sqlite_file)
    con.execute(f'CREATE TABLE "{hostile_name}" (a INTEGER)')
    con.execute(f'INSERT INTO "{hostile_name}" VALUES (1)')
    con.commit()
    con.close()

    with TabularEngine() as engine:
        # Registration may legitimately fail (DuckDB rejecting a
        # malformed identifier or an unhappy CREATE TABLE) — that is
        # acceptable. What is NOT acceptable is the injected
        # ``CREATE TABLE pwned`` succeeding.
        with contextlib.suppress(duckdb.Error):
            engine.register_file(sqlite_file, table_name="hostile")

        # Whether registration succeeded or not, the injected
        # ``pwned`` table must NOT exist in the engine.
        names = {t["name"] for t in engine.list_tables()}
        assert "pwned" not in names, (
            f"SQL injection succeeded: 'pwned' table appeared in engine. "
            f"Tables present: {sorted(names)}"
        )
