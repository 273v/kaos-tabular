"""Tabular engine error hierarchy for kaos-tabular.

All errors subclass KaosTabularError → KaosCoreError, carrying structured
details for agent-friendly error messages and middleware decision-making.
"""

from __future__ import annotations

from kaos_core.exceptions import KaosCoreError


class KaosTabularError(KaosCoreError):
    """Base error for all kaos-tabular operations."""


class EngineError(KaosTabularError):
    """DuckDB engine initialization or lifecycle error."""


class QueryError(KaosTabularError):
    """SQL query execution failed (syntax error, missing table, etc.)."""


class RegistrationError(KaosTabularError):
    """File or table registration failed (unsupported format, file not found, etc.)."""
