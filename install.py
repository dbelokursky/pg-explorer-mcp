#!/usr/bin/env python3
"""Register pg-mcp in GigaCode CLI (or Qwen Code) and check that it starts.

    python3 install.py            register in ~/.gigacode/settings.json, then check
    python3 install.py --check    only start the server and call status
    python3 install.py --client qwen

Uses only the standard library and registers the very interpreter that runs it,
so the agent later starts the server on a Python that is known to work. PG*
variables set in the current shell, except PGPASSWORD, are copied into the
server entry; the password belongs in the libpq password file (~/.pgpass).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List

MIN_PYTHON = (3, 9)
ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_PY = os.path.join(ROOT, "server", "run.py")
SERVER_NAME = "pg"
SETTINGS_FILES = {"gigacode": "~/.gigacode/settings.json", "qwen": "~/.qwen/settings.json"}
COPIED_ENV = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGSSLMODE",
    "PGSSLROOTCERT",
    "PGPASSFILE",
    "PG_MCP_STATEMENT_TIMEOUT_MS",
)


def server_entry() -> Dict[str, Any]:
    entry: Dict[str, Any] = {"command": sys.executable, "args": ["-I", RUN_PY], "timeout": 30000}
    env = {name: os.environ[name] for name in COPIED_ENV if os.environ.get(name)}
    if env:
        entry["env"] = env
    return entry


def register(settings_path: str, entry: Dict[str, Any]) -> None:
    settings: Any = {}
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as handle:
            text = handle.read()
        try:
            settings = json.loads(text) if text.strip() else {}
        except ValueError:
            settings = None
        if not isinstance(settings, dict) or not isinstance(settings.get("mcpServers", {}), dict):
            snippet = json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, ensure_ascii=False)
            sys.exit(f"{settings_path} is not plain JSON (comments?). Merge this into it by hand:\n{snippet}")
        backup = f"{settings_path}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(settings_path, backup)
        print(f"backup of the old settings: {backup}")
    settings.setdefault("mcpServers", {})[SERVER_NAME] = entry
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    temporary = settings_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, settings_path)
    print(f"registered MCP server {SERVER_NAME!r} in {settings_path}")


def check(entry: Dict[str, Any]) -> bool:
    """Start the server the way the agent will and call its status tool."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pg-mcp install.py"}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
    ]
    payload = "".join(json.dumps(message) + "\n" for message in messages).encode("utf-8")
    env = dict(os.environ, **entry.get("env", {}))
    try:
        proc = subprocess.run(
            [entry["command"], *entry["args"]], input=payload, capture_output=True, timeout=60, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"the server did not run: {exc}")
        return False
    replies: Dict[Any, Dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        replies[message.get("id")] = message
    initialize, status = replies.get(1, {}), replies.get(2, {})
    if "result" not in initialize or "result" not in status:
        print("the server did not answer as expected:")
        print(proc.stderr.decode("utf-8", "replace").strip())
        return False
    text = status["result"]["content"][0]["text"]
    print(f"protocol {initialize['result']['protocolVersion']}")
    print(text)
    return "database: connected" in text


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Register the pg-mcp server in the agent's settings and check it.")
    parser.add_argument("--client", choices=sorted(SETTINGS_FILES), default="gigacode", help="whose settings.json to edit")
    parser.add_argument("--check", action="store_true", help="only start the server and call status; change nothing")
    args = parser.parse_args(argv)

    entry = server_entry()
    if not args.check:
        register(os.path.expanduser(SETTINGS_FILES[args.client]), entry)
    connected = check(entry)
    if not args.check:
        print(f"\nnext: {args.client} extensions link {ROOT}")
        print("then restart the agent so it starts the server with the new settings")
    return 0 if connected else 1


if __name__ == "__main__":
    if sys.version_info < MIN_PYTHON:
        sys.exit("pg-mcp needs Python %d.%d or newer, this is %s" % (MIN_PYTHON + (sys.version.split()[0],)))
    sys.exit(main(sys.argv[1:]))
