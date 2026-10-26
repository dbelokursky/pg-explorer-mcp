"""scripts/client_smoke.py reads what Qwen Code prints; these are the outputs it has to understand."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import client_smoke
import pytest

MCP_LIST = (
    "Configured MCP servers:\n"
    "\x1b[32m✓\x1b[0m pg: /usr/bin/python3 -I /opt/pg-explorer-mcp/run.py (stdio) - Connected\n"
    "\x1b[31m✗\x1b[0m pg2: /usr/bin/python3 -I /opt/other/run.py (stdio) - Disconnected\n"
)
EXTENSIONS = """✓ pg-explorer-mcp (0.3.0)
 Description: Read-only PostgreSQL access for the agent.
 Context files:
  /opt/pg-explorer-mcp/pg-explorer-mcp-context.md
 Commands:
  /pg:setup
"""


def test_a_connected_server_is_recognised_through_the_colours() -> None:
    assert client_smoke.server_connected(MCP_LIST)


@pytest.mark.parametrize("name", ["pg2", "pg-sales", "p"])
def test_other_or_disconnected_servers_do_not_count(name: str) -> None:
    assert not client_smoke.server_connected(MCP_LIST, name)


def manifest(version: str = "0.3.0", name: str = "pg-explorer-mcp") -> dict[str, str]:
    return {"name": name, "version": version, "contextFileName": f"{name}-context.md"}


def test_the_extension_is_checked_for_name_version_context_and_command() -> None:
    assert client_smoke.extension_missing(EXTENSIONS, manifest()) == []
    assert client_smoke.extension_missing(EXTENSIONS, manifest("0.4.0")) == ["pg-explorer-mcp (0.4.0)"]
    assert client_smoke.extension_missing(EXTENSIONS.replace("/pg:setup", ""), manifest()) == ["/pg:setup"]


def test_the_name_and_context_file_come_from_the_manifest() -> None:
    assert client_smoke.extension_missing(EXTENSIONS, manifest(name="pg-mcp")) == [
        "pg-mcp (0.3.0)",
        "pg-mcp-context.md",
    ]


def test_the_password_moves_into_a_password_file(tmp_path: Path) -> None:
    env = client_smoke.client_env(str(tmp_path), {"PGPASSWORD": r"se:cr\et", "PGHOST": "db"})
    assert "PGPASSWORD" not in env
    assert (env["HOME"], env["USERPROFILE"], env["PGHOST"]) == (str(tmp_path), str(tmp_path), "db")
    passfile = Path(env["PGPASSFILE"])
    assert passfile.read_text(encoding="utf-8") == "*:*:*:*:se\\:cr\\\\et\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(passfile.stat().st_mode) == 0o600


def test_without_a_password_no_password_file_is_made(tmp_path: Path) -> None:
    env = client_smoke.client_env(str(tmp_path), {"PGHOST": "db"})
    assert "PGPASSFILE" not in env
    assert list(tmp_path.iterdir()) == []
