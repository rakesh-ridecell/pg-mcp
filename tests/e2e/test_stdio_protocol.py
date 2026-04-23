"""End-to-end stdio protocol tests.

Launches ``pg-mcp serve`` as a subprocess, speaks MCP JSON-RPC on
stdin/stdout, and asserts both the handshake and that tools enumerate
cleanly. No Postgres is required — the configured connection
intentionally points at a dead address so pool opens fail in the
background, which the server must tolerate.
"""

from __future__ import annotations

import json
import select
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture()
def dead_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
        log_file: {log}
        log_sql: hash
        connections:
          - name: dead
            host: 127.0.0.1
            port: 1
            database: nope
            user: nobody
            password: nope
            connect_timeout: 1
            pool:
              min_size: 0
              max_size: 1
        """.replace("{log}", str(tmp_path / "audit.jsonl")),
        encoding="utf-8",
    )
    return cfg


def _pg_mcp_bin() -> str:
    exe = shutil.which("pg-mcp")
    if exe:
        return exe
    # Fall back to the in-repo venv.
    venv = Path(__file__).parent.parent.parent / ".venv" / "bin" / "pg-mcp"
    if venv.exists():
        return str(venv)
    pytest.skip("pg-mcp binary not found on PATH or in .venv")


def _send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload).encode() + b"\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen, *, timeout: float = 8.0) -> dict | None:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        return None
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line.decode())


def test_stdio_handshake_and_tool_list(dead_config: Path) -> None:
    proc = subprocess.Popen(
        [_pg_mcp_bin(), "--config", str(dead_config), "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1"},
                },
            },
        )
        resp = _recv(proc)
        assert resp is not None, "no response to initialize"
        assert resp["result"]["serverInfo"]["name"] == "pg-mcp"

        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = _recv(proc)
        assert resp is not None
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "list_connections",
            "list_schemas",
            "list_tables",
            "list_views",
            "describe_table",
            "describe_view",
            "sample_rows",
            "run_query",
            "explain_query",
            "search_schema",
            "table_stats",
        }, f"unexpected tool set: {names}"

        # list_connections must respond even though the only connection
        # points at a dead address.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_connections", "arguments": {}},
            },
        )
        resp = _recv(proc)
        assert resp is not None
        content = resp["result"]["content"]
        assert content, "list_connections returned no content"
        text = content[0]["text"]
        assert "dead" in text  # connection name shows up
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)


def test_only_json_frames_on_stdout(dead_config: Path) -> None:
    """Whatever the server writes to stdout must be valid JSON-RPC frames.

    Anything else — a stray `print`, a traceback, psycopg noise — would
    corrupt the MCP stream. We fuzz this by reading every line the
    server emits during a handshake and proving each is valid JSON.
    """
    proc = subprocess.Popen(
        [_pg_mcp_bin(), "--config", str(dead_config), "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        _recv(proc)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # Issue several rapid calls and verify each response is valid JSON.
        for i in range(5):
            _send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 10 + i,
                    "method": "tools/call",
                    "params": {"name": "list_connections", "arguments": {}},
                },
            )
        for _ in range(5):
            resp = _recv(proc)
            assert resp is not None, "missing response"
            # Must be a valid dict with jsonrpc marker.
            assert resp.get("jsonrpc") == "2.0"
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
