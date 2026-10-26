"""Read-only PostgreSQL MCP server built on the Python standard library."""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.7.0"

# Pure-Python dependencies ship inside the package (see _vendor/README.md), so the
# server runs from a plain checkout without pip or network access.
_VENDOR = str(Path(__file__).resolve().parent / "_vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
