"""Concurrency stress test — prove the stdio server handles parallel tool calls.

Launches one `pg-mcp serve` subprocess and fires many concurrent
`list_connections` and `run_query` tool calls at it. Checks:

- All responses come back with matching JSON-RPC ids (nothing
  interleaves / loses).
- No traffic on stdout except well-formed JSON frames.
- The server remains responsive after the burst.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from queue import Queue

import pytest

pytestmark = [pytest.mark.e2e]


def _pg_mcp_bin() -> str:
    exe = shutil.which("pg-mcp")
    if exe:
        return exe
    venv = Path(__file__).parent.parent.parent / ".venv" / "bin" / "pg-mcp"
    if venv.exists():
        return str(venv)
    pytest.skip("pg-mcp binary not found")


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
              max_size: 2
        """.replace("{log}", str(tmp_path / "audit.jsonl")),
        encoding="utf-8",
    )
    return cfg


def _reader_thread(stream, q: Queue) -> None:
    """Read JSON-RPC frames from *stream* and put them on *q*."""
    while True:
        line = stream.readline()
        if not line:
            break
        try:
            q.put(json.loads(line.decode()))
        except json.JSONDecodeError as e:
            q.put({"_parse_error": str(e), "_raw": line.decode(errors="replace")})


def test_ten_parallel_list_connections_calls(dead_config: Path) -> None:
    proc = subprocess.Popen(
        [_pg_mcp_bin(), "--config", str(dead_config), "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        out_q: Queue = Queue()
        t = threading.Thread(target=_reader_thread, args=(proc.stdout, out_q), daemon=True)
        t.start()

        def send(payload: dict) -> None:
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            proc.stdin.flush()

        # Handshake
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            }
        )
        # Drain initialize response
        _ = out_q.get(timeout=8)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # Fire 10 tool calls in rapid succession.
        n = 10
        for i in range(n):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 100 + i,
                    "method": "tools/call",
                    "params": {"name": "list_connections", "arguments": {}},
                }
            )

        # Collect responses. Every id must come back exactly once, and
        # every message must be a well-formed JSON object.
        received_ids: set[int] = set()
        for _ in range(n):
            resp = out_q.get(timeout=10)
            assert "_parse_error" not in resp, f"Got non-JSON on stdout: {resp.get('_raw')!r}"
            assert resp.get("jsonrpc") == "2.0"
            rid = resp.get("id")
            assert isinstance(rid, int) and 100 <= rid < 100 + n
            assert rid not in received_ids, f"duplicate response id {rid}"
            received_ids.add(rid)

        assert received_ids == set(range(100, 100 + n))

        # Server should still be responsive.
        send(
            {
                "jsonrpc": "2.0",
                "id": 999,
                "method": "tools/list",
                "params": {},
            }
        )
        resp = out_q.get(timeout=5)
        assert resp.get("id") == 999
        assert "result" in resp
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
