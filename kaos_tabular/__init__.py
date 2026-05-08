"""kaos-tabular: DuckDB-powered tabular data engine for KAOS."""

from kaos_tabular._version import __version__
from kaos_tabular.engine import EngineEvent, TabularEngine
from kaos_tabular.errors import EngineError, KaosTabularError, QueryError, RegistrationError
from kaos_tabular.readers import read_csv, read_json, read_parquet

__all__ = [
    "EngineError",
    "EngineEvent",
    "KaosTabularError",
    "QueryError",
    "RegistrationError",
    "TabularEngine",
    "__version__",
    "read_csv",
    "read_json",
    "read_parquet",
]
