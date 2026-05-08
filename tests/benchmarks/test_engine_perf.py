"""Wall-clock performance benchmarks for kaos-tabular.

Relocated from ``tests/unit/test_adversarial.py`` per audit-01 KTAB-006:
benchmark assertions don't belong in the bounded unit gate. Run
explicitly with::

    pytest tests/benchmarks -m benchmark

The numeric thresholds are regression catches, not absolute SLAs — they
were calibrated on the developer workstation when the suite landed.
Tighten or relax with care.
"""

from __future__ import annotations

import csv
import time
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

from kaos_tabular.cli import main as cli_main
from kaos_tabular.engine import TabularEngine

pytestmark = pytest.mark.benchmark


def _generate_large_csv(path: Path, n_rows: int, n_cols: int = 5) -> None:
    """Generate a CSV with ``n_rows`` rows and ``n_cols`` integer columns."""
    headers = [f"col_{i}" for i in range(n_cols)]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i in range(n_rows):
            w.writerow([str(i * n_cols + j) for j in range(n_cols)])


class TestPerformanceBenchmarks:
    """Wall-clock benchmarks with assertions. These catch regressions."""

    def test_register_10k_rows_under_1s(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "bench10k.csv"
        _generate_large_csv(csv_file, n_rows=10_000)

        start = time.monotonic()
        with TabularEngine() as engine:
            engine.register_file(csv_file)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"10K register: {elapsed:.3f}s"

    def test_aggregate_10k_rows_under_100ms(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "bench_agg.csv"
        _generate_large_csv(csv_file, n_rows=10_000)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="data")

            start = time.monotonic()
            engine.execute("SELECT COUNT(*), SUM(CAST(col_0 AS BIGINT)) FROM data")
            elapsed = time.monotonic() - start

        assert elapsed < 0.1, f"10K aggregate: {elapsed:.3f}s"

    def test_describe_under_100ms(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "bench_desc.csv"
        _generate_large_csv(csv_file, n_rows=10_000)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="data")

            start = time.monotonic()
            engine.describe_table("data")
            elapsed = time.monotonic() - start

        assert elapsed < 0.1, f"Describe: {elapsed:.3f}s"

    def test_sample_under_50ms(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "bench_sample.csv"
        _generate_large_csv(csv_file, n_rows=10_000)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="data")

            start = time.monotonic()
            result = engine.sample("data", n=10)
            elapsed = time.monotonic() - start

        assert len(result.rows) == 10
        assert elapsed < 0.05, f"Sample: {elapsed:.3f}s"

    def test_cli_query_under_3s(self, tmp_path: Path) -> None:
        """CLI end-to-end: parse args, register, query, format, output."""
        csv_file = tmp_path / "bench_cli.csv"
        _generate_large_csv(csv_file, n_rows=10_000)

        start = time.monotonic()
        with mock.patch("sys.stdout", new_callable=StringIO):
            cli_main(
                [
                    "query",
                    str(csv_file),
                    "SELECT COUNT(*) as cnt FROM bench_cli",
                    "--json",
                ]
            )
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"CLI query: {elapsed:.3f}s"

    def test_50_sequential_queries(self, tmp_path: Path) -> None:
        """50 queries against same table — measures per-query overhead."""
        csv_file = tmp_path / "bench_seq.csv"
        _generate_large_csv(csv_file, n_rows=1_000)

        with TabularEngine() as engine:
            engine.register_file(csv_file, table_name="data")

            start = time.monotonic()
            for i in range(50):
                engine.execute(f"SELECT * FROM data WHERE CAST(col_0 AS INTEGER) = {i}")
            elapsed = time.monotonic() - start

        per_query = elapsed / 50
        assert per_query < 0.05, f"Per-query: {per_query:.3f}s (50 queries in {elapsed:.2f}s)"
