"""Config loading, env-var expansion, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from pg_mcp.config import discover_config_path, load_config
from pg_mcp.errors import ConfigError


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_basic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASS", "s3cret")
    p = _write(
        tmp_path,
        """
        connections:
          - name: prod
            dsn: postgresql://u@host:5432/db
            password: ${DB_PASS}
        """,
    )
    cfg = load_config(p)
    assert cfg.connections[0].name == "prod"
    assert cfg.connections[0].password == "s3cret"
    assert cfg.defaults.row_limit == 1000
    assert cfg.log_sql == "hash"


def test_default_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    p = _write(
        tmp_path,
        """
        connections:
          - name: dev
            host: localhost
            database: d
            user: u
            password: ${MISSING_VAR:-fallback}
        """,
    )
    cfg = load_config(p)
    assert cfg.connections[0].password == "fallback"


def test_empty_env_value_is_expanded_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMPTY", "")
    p = _write(
        tmp_path,
        """
        connections:
          - name: c
            host: h
            database: d
            user: u
            password: X${EMPTY}Y
        """,
    )
    cfg = load_config(p)
    assert cfg.connections[0].password == "XY"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_missing_env_var_no_default_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOTALLY_MISSING", raising=False)
    p = _write(
        tmp_path,
        """
        connections:
          - name: c
            dsn: postgresql://u:${TOTALLY_MISSING}@h/d
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    assert "TOTALLY_MISSING" in str(excinfo.value)
    assert "connections" in str(excinfo.value)


def test_duplicate_connection_names(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        connections:
          - name: dup
            dsn: postgresql://a/b
          - name: dup
            dsn: postgresql://c/d
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    assert "duplicate" in str(excinfo.value).lower()


def test_invalid_connection_name(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        connections:
          - name: "has spaces"
            dsn: postgresql://a/b
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_connection_name_starting_with_digit(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        connections:
          - name: 1prod
            dsn: postgresql://a/b
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_dsn_and_host(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        connections:
          - name: bad
            port: 5432
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    # Must indicate the required fields
    assert "dsn" in str(excinfo.value) or "host" in str(excinfo.value)


def test_no_connections(tmp_path: Path) -> None:
    p = _write(tmp_path, "connections: []\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_empty_file(tmp_path: Path) -> None:
    p = _write(tmp_path, "")
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    assert "empty" in str(excinfo.value).lower()


def test_not_a_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path, "- 1\n- 2\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_bad_yaml(tmp_path: Path) -> None:
    p = _write(tmp_path, "connections:\n  - name: {unclosed")
    with pytest.raises(ConfigError):
        load_config(p)


def test_duplicate_keys_in_yaml_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        log_sql: hash
        log_sql: full
        connections:
          - name: a
            dsn: postgresql://h/d
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_pool_sizes_inverted(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        connections:
          - name: a
            dsn: postgresql://h/d
            pool:
              min_size: 5
              max_size: 2
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_statement_timeout_out_of_range(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        defaults:
          statement_timeout_ms: 10
        connections:
          - name: a
            dsn: postgresql://h/d
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


# ---------------------------------------------------------------------------
# sslrootcert relative path resolution
# ---------------------------------------------------------------------------


def test_relative_ssl_paths_resolved(tmp_path: Path) -> None:
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "root.pem").write_text("fake")
    p = _write(
        tmp_path,
        """
        connections:
          - name: a
            dsn: postgresql://h/d
            sslrootcert: ./certs/root.pem
        """,
    )
    cfg = load_config(p)
    assert cfg.connections[0].sslrootcert == str((tmp_path / "certs" / "root.pem").resolve())


# ---------------------------------------------------------------------------
# discover_config_path precedence
# ---------------------------------------------------------------------------


def test_discover_cli_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PG_MCP_CONFIG", raising=False)
    p = _write(tmp_path, "connections: [{name: a, dsn: postgresql://h/d}]")
    assert discover_config_path(p) == p


def test_discover_env_var_used_when_no_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(tmp_path, "connections: []")
    monkeypatch.setenv("PG_MCP_CONFIG", str(p))
    assert discover_config_path(None) == p


def test_discover_returns_none_if_nothing_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PG_MCP_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "doesnotexist"))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Must not find anything (dirs don't exist)
    result = discover_config_path(None)
    assert result is None


def test_cli_override_with_missing_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PG_MCP_CONFIG", raising=False)
    assert discover_config_path(tmp_path / "nope.yaml") is None
