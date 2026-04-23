"""Error taxonomy for pg-mcp.

Every error that reaches the MCP client has a stable ``code`` string so the
LLM can reason about remediation. Internal exceptions are logged with full
detail; the message surfaced to the model is intentionally concise.
"""

from __future__ import annotations


class PgMcpError(Exception):
    """Base class for all pg-mcp errors.

    The ``code`` is a stable identifier documented in the README; the
    ``detail`` is a short human-readable elaboration.
    """

    code: str = "internal_error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.detail}" if self.detail else self.code


class ConfigError(PgMcpError):
    """Fatal config-loading error. Raised only during startup."""

    code = "config_error"


class ToolInputError(PgMcpError):
    code = "invalid_parameter"


class UnknownConnectionError(PgMcpError):
    code = "unknown_connection"


class ConnectionUnavailableError(PgMcpError):
    code = "connection_unavailable"


class ConnectionUnsafeError(PgMcpError):
    """The RO probe did not get SQLSTATE 25006 — connection is not read-only."""

    code = "connection_unsafe"


class PoolExhaustedError(PgMcpError):
    code = "connection_pool_exhausted"


class RateLimitedError(PgMcpError):
    """Raised when a connection's per-minute rate limit is exceeded."""

    code = "rate_limited"


class PolicyViolation(PgMcpError):  # noqa: N818 — "Violation" is idiomatic for policy errors
    """The SQL was rejected by the safety policy without reaching Postgres.

    ``code`` is always ``sql_rejected_by_policy``; ``reason`` is the
    sub-code (``disallowed_statement``, ``disallowed_function``,
    ``multiple_statements_not_allowed``, etc.); ``detail`` names the
    offending construct.
    """

    code = "sql_rejected_by_policy"

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class QueryTimeoutError(PgMcpError):
    code = "query_timeout"


class PostgresError(PgMcpError):
    """Wraps a psycopg error; carries the SQLSTATE."""

    code = "postgres_error"

    def __init__(self, detail: str, sqlstate: str | None = None) -> None:
        super().__init__(detail)
        self.sqlstate = sqlstate


class ResultTooLargeError(PgMcpError):
    """Raised only in preamble flags; not an outright failure."""

    code = "result_too_large"


__all__ = [
    "ConfigError",
    "ConnectionUnavailableError",
    "ConnectionUnsafeError",
    "PgMcpError",
    "PolicyViolation",
    "PoolExhaustedError",
    "PostgresError",
    "QueryTimeoutError",
    "RateLimitedError",
    "ResultTooLargeError",
    "ToolInputError",
    "UnknownConnectionError",
]
