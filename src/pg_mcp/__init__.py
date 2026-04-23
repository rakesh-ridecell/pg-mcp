"""pg-mcp — read-only PostgreSQL MCP server.

Import side-effects are intentionally minimal here to keep `pg-mcp --help`
fast and avoid pulling heavy optional deps until they are needed.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
