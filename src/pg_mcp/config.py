"""Config file loading, env-var expansion, and validation.

Discovery precedence:

1. ``--config`` CLI flag
2. ``PG_MCP_CONFIG`` env var
3. ``$XDG_CONFIG_HOME/pg-mcp/config.yaml``
4. ``~/.config/pg-mcp/config.yaml``

Env var expansion supports ``${VAR}`` and ``${VAR:-default}``. Bare
``$VAR`` (no braces) is intentionally *not* supported; literal ``$``
passes through unchanged.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from pg_mcp.errors import ConfigError

# ----- env-var expansion ----------------------------------------------------

# Matches ${VAR} or ${VAR:-default}.
_ENV_REF = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def _expand_env_in_str(value: str, *, path: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in *value*.

    Raises :class:`ConfigError` if a referenced var is unset and has no
    default. *path* is a dotted path into the YAML (for the error msg).
    """

    def _repl(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        raise ConfigError(f"{path} references ${{{name}}} which is not set (and has no default)")

    return _ENV_REF.sub(_repl, value)


def _expand_env(obj: Any, *, path: str = "$") -> Any:
    """Recursively walk *obj*, expanding env refs in every string value."""
    if isinstance(obj, str):
        return _expand_env_in_str(obj, path=path)
    if isinstance(obj, Mapping):
        return {k: _expand_env(v, path=f"{path}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v, path=f"{path}[{i}]") for i, v in enumerate(obj)]
    return obj


# ----- strict YAML loader (no duplicate keys) --------------------------------


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys in mappings."""


def _no_duplicates(
    loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)  # type: ignore[no-untyped-call]
    return mapping


_StrictSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


# ----- Pydantic models ------------------------------------------------------

_CONNECTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class PoolSettings(BaseModel):
    min_size: int = Field(default=1, ge=0, le=32)
    max_size: int = Field(default=5, ge=1, le=32)

    @model_validator(mode="after")
    def _check_sizes(self) -> PoolSettings:
        if self.max_size < self.min_size:
            raise ValueError(f"pool.max_size ({self.max_size}) < min_size ({self.min_size})")
        return self


class Defaults(BaseModel):
    statement_timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    row_limit: int = Field(default=1000, ge=1, le=100_000)
    byte_limit: int = Field(default=1_048_576, ge=1024, le=100 * 1024 * 1024)
    cell_limit: int = Field(default=8192, ge=64)
    acquire_timeout_s: float = Field(default=5.0, gt=0, le=60)

    @model_validator(mode="after")
    def _cell_le_byte(self) -> Defaults:
        if self.cell_limit > self.byte_limit:
            raise ValueError(
                f"defaults.cell_limit ({self.cell_limit}) > byte_limit ({self.byte_limit})"
            )
        return self


class ConnectionConfig(BaseModel):
    name: str
    description: str | None = None
    dsn: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    user: str | None = None
    password: str | None = None
    sslmode: str | None = None
    sslrootcert: str | None = None
    sslcert: str | None = None
    sslkey: str | None = None
    connect_timeout: int | None = Field(default=5, ge=1, le=120)
    statement_timeout_ms: int | None = Field(default=None, ge=100, le=600_000)
    row_limit: int | None = Field(default=None, ge=1, le=100_000)
    search_path: list[str] | None = None
    pool: PoolSettings = Field(default_factory=PoolSettings)

    # Per-connection schema visibility. Applied as a post-filter to
    # every introspection tool AND as a pre-flight parser check on any
    # run_query/explain_query that references tables — the query is
    # rejected before it reaches Postgres if it touches a schema not in
    # the allow-list or in the deny-list. Both optional; if both are
    # unset the role's grants are the only gate.
    allowed_schemas: list[str] | None = None
    denied_schemas: list[str] | None = None

    @field_validator("allowed_schemas", "denied_schemas")
    @classmethod
    def _validate_schema_lists(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("must be a list of schema names")
        cleaned: list[str] = []
        for s in v:
            if not isinstance(s, str) or not s:
                raise ValueError(f"schema name must be a non-empty string, got {s!r}")
            cleaned.append(s)
        return cleaned

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _CONNECTION_NAME_RE.match(v):
            raise ValueError(
                f"connection name {v!r} must match "
                r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
            )
        return v

    @model_validator(mode="after")
    def _require_dsn_or_host(self) -> ConnectionConfig:
        if self.dsn:
            return self
        if not (self.host and self.database and self.user):
            raise ValueError("must provide either 'dsn' or all of 'host', 'database', 'user'")
        return self


class Config(BaseModel):
    log_file: str | None = None
    log_sql: Literal["hash", "redacted", "full"] = "hash"
    defaults: Defaults = Field(default_factory=Defaults)
    connections: list[ConnectionConfig]

    @model_validator(mode="after")
    def _unique_names(self) -> Config:
        seen: set[str] = set()
        for c in self.connections:
            if c.name in seen:
                raise ValueError(f"duplicate connection name {c.name!r}")
            seen.add(c.name)
        if not self.connections:
            raise ValueError("at least one connection must be defined")
        return self

    def get(self, name: str) -> ConnectionConfig | None:
        for c in self.connections:
            if c.name == name:
                return c
        return None


# ----- loading --------------------------------------------------------------


def discover_config_path(
    cli_override: str | Path | None = None,
) -> Path | None:
    """Walk the discovery precedence and return the first existing path.

    Returns ``None`` if nothing is found; the caller decides whether that
    is fatal or handled (e.g., by ``pg-mcp init``).
    """
    if cli_override is not None:
        p = Path(cli_override).expanduser()
        return p if p.exists() else None

    env_path = os.environ.get("PG_MCP_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        p = Path(xdg) / "pg-mcp" / "config.yaml"
        if p.exists():
            return p

    home_default = Path.home() / ".config" / "pg-mcp" / "config.yaml"
    if home_default.exists():
        return home_default

    return None


def load_config(path: Path) -> Config:
    """Load, expand env vars, and validate the config at *path*."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}") from e

    try:
        raw = yaml.load(raw_text, Loader=_StrictSafeLoader)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e

    if raw is None:
        raise ConfigError(f"config file {path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config file {path} must be a YAML mapping at top level, got {type(raw).__name__}"
        )

    expanded = _expand_env(raw, path="$")

    try:
        cfg = Config.model_validate(expanded)
    except Exception as e:
        raise ConfigError(f"config validation failed: {e}") from e

    # Resolve sslrootcert-like paths relative to the config file's directory.
    config_dir = path.parent
    for conn in cfg.connections:
        for field in ("sslrootcert", "sslcert", "sslkey"):
            value = getattr(conn, field)
            if value and not Path(value).is_absolute():
                setattr(conn, field, str((config_dir / value).resolve()))

    return cfg


__all__ = [
    "Config",
    "ConnectionConfig",
    "Defaults",
    "PoolSettings",
    "discover_config_path",
    "load_config",
]
