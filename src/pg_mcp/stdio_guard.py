"""Keep stray output off stdout.

The MCP stdio transport uses stdout exclusively for JSON-RPC frames. A
stray ``print()``, debug output, or third-party library warning sent to
stdout will corrupt the protocol.

This module provides two complementary mechanisms:

1. :func:`configure_logging_to_stderr` — installs a stdlib ``logging``
   handler that writes to stderr, never stdout. Called by
   ``pg-mcp serve`` before anything else.

2. :func:`install_strict_guard` — for tests only. Replaces ``sys.stdout``
   with a stream that raises ``RuntimeError`` on write, so the test
   suite can prove no code path writes to stdout outside the MCP framer.
"""

from __future__ import annotations

import io
import logging
import sys
from typing import TextIO


def configure_logging_to_stderr(level: str = "INFO") -> None:
    """Route all stdlib logging to stderr. Safe to call multiple times."""
    root = logging.getLogger()
    # Remove any handlers that might write to stdout.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)


class _StrictStdout(io.TextIOBase):
    """A stdout replacement that raises on every write. Tests only."""

    def __init__(self, real: TextIO) -> None:
        self._real = real

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:  # type: ignore[override]
        raise RuntimeError(
            "Unexpected write to stdout during a strict-guard test; this "
            "would corrupt the MCP JSON-RPC stream.\n"
            f"Attempted to write: {s!r}"
        )

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return self._real.fileno()


_original_stdout: TextIO | None = None


def install_strict_guard() -> None:
    """Install a stdout that raises on any write. For tests only."""
    global _original_stdout
    if _original_stdout is not None:
        return
    _original_stdout = sys.stdout
    sys.stdout = _StrictStdout(_original_stdout)  # type: ignore[assignment]


def uninstall_strict_guard() -> None:
    global _original_stdout
    if _original_stdout is None:
        return
    sys.stdout = _original_stdout
    _original_stdout = None


__all__ = [
    "configure_logging_to_stderr",
    "install_strict_guard",
    "uninstall_strict_guard",
]
