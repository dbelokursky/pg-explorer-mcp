#!/usr/bin/env python3
"""Register pg-mcp in GigaCode CLI (or Qwen Code) and check that it starts.

    python3 install.py            register in ~/.gigacode/settings.json, then check
    python3 install.py --check    only start the server and call status
    python3 install.py --client qwen
    python3 install.py --lang ru  the server and this script speak Russian

Uses only the standard library and registers the very interpreter that runs it,
so the agent later starts the server on a Python that is known to work. PG*
variables set in the current shell, except PGPASSWORD, are copied into the
server entry; the password belongs in the libpq password file (~/.pgpass).
The language goes into the entry as well: an agent may start the server
without the locale variables of this shell.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from pg_mcp import i18n
from pg_mcp.i18n import t

MIN_PYTHON = (3, 9)
ROOT = Path(__file__).resolve().parent
RUN_PY = str(ROOT / "run.py")
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
    "PG_MCP_MAX_RESPONSE_CHARS",
    "PG_MCP_PROFILE",
    "PG_MCP_QUERY_LOG",
)
PROFILES = ("schema", "explore", "analyst")


def server_entry(profile: str | None = None, language: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": sys.executable, "args": ["-I", RUN_PY], "timeout": 30000}
    env = {name: os.environ[name] for name in COPIED_ENV if os.environ.get(name)}
    if profile:
        env["PG_MCP_PROFILE"] = profile
    if language:
        env["PG_MCP_LANG"] = language
    if env:
        entry["env"] = env
    return entry


def register(settings_path: str | Path, entry: dict[str, Any]) -> None:
    path = Path(settings_path)
    settings: Any = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            settings = json.loads(text) if text.strip() else {}
        except ValueError:
            settings = None
        if not isinstance(settings, dict) or not isinstance(settings.get("mcpServers", {}), dict):
            snippet = json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, ensure_ascii=False)
            sys.exit(t("install.not_json", path=path, snippet=snippet))
        backup = path.with_name(f"{path.name}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        print(t("install.backup", path=backup))
    settings.setdefault("mcpServers", {})[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(t("install.registered", name=SERVER_NAME, path=path))


def check(entry: dict[str, Any]) -> bool:
    """Start the server the way the agent will and call its status tool."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pg-mcp install.py"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
    ]
    payload = "".join(json.dumps(message) + "\n" for message in messages).encode("utf-8")
    env = dict(os.environ, **entry.get("env", {}))
    try:
        proc = subprocess.run(
            [entry["command"], *entry["args"]], input=payload, capture_output=True, timeout=60, env=env, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(t("install.did_not_run", error=exc))
        return False
    replies: dict[Any, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        replies[message.get("id")] = message
    initialize, status = replies.get(1, {}), replies.get(2, {})
    if "result" not in initialize or "result" not in status:
        print(t("install.no_answer"))
        print(proc.stderr.decode("utf-8", "replace").strip())
        return False
    text: str = status["result"]["content"][0]["text"]
    print(t("install.protocol", version=initialize["result"]["protocolVersion"]))
    print(text)
    return connected(text)


def connected(status: str) -> bool:
    """Whether the status text reports a connection, in whichever language the server speaks."""
    markers = tuple(i18n.CATALOG[language]["status.connected"].partition("{")[0] for language in i18n.LANGUAGES)
    return any(line.startswith(markers) for line in status.splitlines())


def main(argv: list[str]) -> int:
    # The help texts are in the chosen language, so --lang is read before the parser is built.
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--lang", choices=i18n.LANGUAGES)
    language = early.parse_known_args(argv)[0].lang or i18n.language_from_env(os.environ, i18n.system_locale())[0]
    i18n.use(language)
    if sys.version_info < MIN_PYTHON:
        sys.exit(t("install.old_python", need=".".join(map(str, MIN_PYTHON)), have=platform.python_version()))

    parser = argparse.ArgumentParser(description=t("install.description"))
    parser.add_argument("--client", choices=sorted(SETTINGS_FILES), default="gigacode", help=t("install.help.client"))
    parser.add_argument("--check", action="store_true", help=t("install.help.check"))
    parser.add_argument("--profile", choices=PROFILES, help=t("install.help.profile"))
    parser.add_argument("--lang", choices=i18n.LANGUAGES, help=t("install.help.lang"))
    args = parser.parse_args(argv)

    entry = server_entry(args.profile, language)
    if not args.check:
        register(Path(SETTINGS_FILES[args.client]).expanduser(), entry)
    ok = check(entry)
    if not args.check:
        print()
        print(t("install.next", client=args.client, path=ROOT))
        print(t("install.restart"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
