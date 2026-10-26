"""Start the pg-mcp server from a checkout, the way MCP clients do: python -I run.py

-I keeps PYTHONPATH, the user's site-packages and the current directory out of
sys.path, so the server imports only its own package and the dependencies
vendored inside it. An installed copy starts with `pg-explorer-mcp` or `python -m pg_explorer_mcp`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from pg_explorer_mcp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
