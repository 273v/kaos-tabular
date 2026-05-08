"""Shared test fixtures for kaos-tabular."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from kaos_tabular.engine import TabularEngine


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture()
def simple_csv(fixtures_dir: Path) -> Path:
    return fixtures_dir / "simple.csv"


@pytest.fixture()
def records_json(fixtures_dir: Path) -> Path:
    return fixtures_dir / "records.json"


@pytest.fixture()
def unicode_csv(fixtures_dir: Path) -> Path:
    return fixtures_dir / "unicode.csv"


@pytest.fixture()
def engine() -> Generator[TabularEngine]:
    eng = TabularEngine()
    yield eng
    eng.close()
