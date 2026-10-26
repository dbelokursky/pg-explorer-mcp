#!/usr/bin/env python3
"""Check pg-explorer-mcp in a real MCP client: Qwen Code, the open-source base of GigaCode CLI.

    python3 scripts/client_smoke.py --qwen "$(command -v qwen)"

No model is needed. In a throwaway home folder the script does what a user
does: registers the server with install.py, asks the client whether it can
reach it (qwen mcp list), links the repository as an extension and checks what
the client makes of it (qwen extensions list). The PG* variables say which
database to use; PGPASSWORD, if set, goes into a password file, where the
README tells users to keep the password.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_S = 120
_COLOURS = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """The text without terminal colours."""
    return _COLOURS.sub("", text)


def server_connected(mcp_list: str, name: str = "pg") -> bool:
    """Whether `qwen mcp list` reports the named server as connected."""
    line = re.compile(rf"\W*{re.escape(name)}: .* - Connected$")
    return any(line.match(entry.strip()) for entry in plain(mcp_list).splitlines())


def extension_missing(extensions: str, manifest: dict[str, str]) -> list[str]:
    """What `qwen extensions list` does not say about the extension of the manifest; empty when it says all."""
    text = plain(extensions)
    wanted = (f"{manifest['name']} ({manifest['version']})", manifest["contextFileName"], "/pg:setup")
    return [item for item in wanted if item not in text]


def client_env(home: str, environ: dict[str, str]) -> dict[str, str]:
    """The environment with a throwaway home and the password moved into a password file."""
    env = dict(environ, HOME=home, USERPROFILE=home, NO_COLOR="1")
    password = env.pop("PGPASSWORD", None)
    if password:
        passfile = Path(home) / ".pgpass"
        escaped = password.replace("\\", "\\\\").replace(":", "\\:")
        passfile.write_text(f"*:*:*:*:{escaped}\n", encoding="utf-8")
        passfile.chmod(0o600)
        env["PGPASSFILE"] = str(passfile)
    return env


def run(command: Sequence[str], env: dict[str, str], stdin: str = "") -> str:
    """Run a command in the throwaway home and return what it printed, without colours."""
    print("$", " ".join(command))
    proc = subprocess.run(
        list(command),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
        env=env,
        cwd=env["HOME"],
        check=False,
    )
    output = plain(proc.stdout + proc.stderr).strip()
    if output:
        print(output)
    if proc.returncode != 0:
        print(f"(exit code {proc.returncode})")
    return output if proc.returncode == 0 else ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check pg-explorer-mcp in a real MCP client, Qwen Code.")
    parser.add_argument("--qwen", required=True, help="path to the qwen executable")
    args = parser.parse_args(argv)
    manifest = json.loads((ROOT / "qwen-extension.json").read_text(encoding="utf-8"))

    failures = []
    with tempfile.TemporaryDirectory(prefix="pg-explorer-mcp-client-") as home:
        env = client_env(home, dict(os.environ))
        install = [sys.executable, str(ROOT / "install.py"), "--client", "qwen", "--profile", "explore", "--lang", "en"]
        if not run(install, env):
            failures.append("install.py did not register the server, or the server did not reach the database")
        if not server_connected(run([args.qwen, "mcp", "list"], env)):
            failures.append("qwen mcp list does not show the pg server as connected")
        if "linked successfully" not in run([args.qwen, "extensions", "link", str(ROOT)], env, stdin="y\n"):
            failures.append("qwen extensions link did not link the repository")
        missing = extension_missing(run([args.qwen, "extensions", "list"], env), manifest)
        if missing:
            failures.append(f"qwen extensions list does not show: {', '.join(missing)}")

    for failure in failures:
        print(f"FAIL: {failure}")
    if not failures:
        print(f"OK: Qwen Code reaches {manifest['name']} {manifest['version']} and sees its extension")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
