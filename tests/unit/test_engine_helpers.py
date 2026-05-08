"""Focused unit tests for engine.py helpers.

The adversarial tests in ``test_sqlite_register.py`` and
``test_path_injection.py`` cover ``_q_lit`` *transitively* via end-to-end
register / save / export round-trips. These tests pin the helper's
contract directly so a subtle change (e.g., switching to ``json.dumps``,
forgetting empty-string handling) fails here before it ships an
exploitable engine.
"""

from __future__ import annotations

import duckdb
import pytest

from kaos_tabular.engine import _q_lit


class TestQLit:
    def test_no_quotes_unchanged(self) -> None:
        assert _q_lit("plain") == "'plain'"

    def test_single_quote_doubled(self) -> None:
        assert _q_lit("o'clock") == "'o''clock'"

    def test_multiple_quotes_each_doubled(self) -> None:
        assert _q_lit("a'b'c") == "'a''b''c'"

    def test_empty_string(self) -> None:
        assert _q_lit("") == "''"

    def test_already_doubled_quotes_get_doubled_again(self) -> None:
        # Idempotency is NOT a property: doubling twice → quadrupling.
        # This is the correct DuckDB behaviour — the input is treated
        # as a raw string, not as a half-escaped literal.
        assert _q_lit("a''b") == "'a''''b'"

    def test_unicode_passes_through(self) -> None:
        assert _q_lit("héllo·世界") == "'héllo·世界'"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "plain",
            "o'clock",
            "/home/user/file with spaces.csv",
            "/path/with'quote.parquet",
            "name'); DROP TABLE x; --",
            "résumé.csv",
        ],
    )
    def test_round_trips_through_duckdb_select(self, raw: str) -> None:
        """Quoted output must be syntactically valid as a DuckDB string
        literal AND round-trip back to the original value when SELECTed.
        """
        con = duckdb.connect(":memory:")
        try:
            sql = f"SELECT {_q_lit(raw)} AS s"
            row = con.execute(sql).fetchone()
            assert row is not None, "DuckDB returned no row for SELECT literal"
            assert row[0] == raw
        finally:
            con.close()
