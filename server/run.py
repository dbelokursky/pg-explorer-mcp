"""Start the pg-mcp server for an MCP client: python -I server/run.py

-I keeps PYTHONPATH, the user's site-packages and the current directory out of
sys.path, so the server imports only its own package and the vendored
dependencies that sit next to this file.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "vendor")]

from pg_mcp.__main__ import main  # noqa: E402  (needs the path set up above)

if __name__ == "__main__":
    sys.exit(main())
