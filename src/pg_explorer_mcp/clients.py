"""Where MCP clients keep their servers, and the entry of pg-mcp in each client's format.

install.py registers the server in a client's file, or prints the entry for the user to paste.
The entry itself is the same everywhere (the interpreter, run.py, the PG* settings); clients only
wrap it differently: most under "mcpServers", VS Code under "servers" with a type, Continue in a
YAML block of its own, and JetBrains AI Assistant in its settings window, so it only gets text.
"""

from __future__ import annotations

import datetime
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MCP_SERVERS = "mcpServers"
VSCODE = "vscode"
CONTINUE = "continue"
PASTE = "paste"


class ClientConfigError(Exception):
    """The client's file cannot be updated safely (not plain JSON, say); the message holds what to add by hand."""


def app_data(env: Mapping[str, str], home: Path) -> Path:
    """Where desktop applications keep their settings on this system."""
    system = sys.platform  # a plain str: type checking on one system then sees the other systems' branches
    if system == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    if system == "darwin":
        return home / "Library" / "Application Support"
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config")


def vscode_user(env: Mapping[str, str], home: Path) -> Path:
    return app_data(env, home) / "Code" / "User"


def xdg_config(env: Mapping[str, str], home: Path) -> Path:
    """~/.config, or what XDG_CONFIG_HOME names, on every system but Windows, where it is %APPDATA%."""
    if sys.platform == "win32":
        return app_data(env, home)
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config")


Locate = Callable[[Mapping[str, str], Path], Path]


@dataclass(frozen=True)
class Client:
    """An MCP client: where its servers live, how it wraps an entry, and how to tell it is installed."""

    key: str  # the value of install.py --client
    label: str
    style: str
    config: Locate | None  # the file to write; None when the client keeps servers in its settings window
    markers: tuple[Locate, ...]  # folders, any of which exists when the client is installed
    extra: Mapping[str, Any] = field(default_factory=dict)  # fields the client adds to every server
    timeout: bool = False  # whether the client reads "timeout" (milliseconds) from an entry

    def path(self, env: Mapping[str, str], home: Path, name: str) -> Path | None:
        if self.config is None:
            return None
        path = self.config(env, home)
        return path / f"{name}.yaml" if self.style == CONTINUE else path

    def present(self, env: Mapping[str, str], home: Path) -> bool:
        return any(marker(env, home).is_dir() for marker in self.markers)


def _home(*parts: str) -> Locate:
    return lambda _env, home: home.joinpath(*parts)


def _vscode(*parts: str) -> Locate:
    return lambda env, home: vscode_user(env, home).joinpath(*parts)


def _app(*parts: str) -> Locate:
    return lambda env, home: app_data(env, home).joinpath(*parts)


def _xdg(*parts: str) -> Locate:
    return lambda env, home: xdg_config(env, home).joinpath(*parts)


def windsurf_config(env: Mapping[str, str], home: Path) -> Path:
    """Windsurf became Devin Desktop in 2026 and its documents name three files; take the one in use.

    The first file that exists wins, then the first whose folder exists, then the newest location.
    """
    candidates = [
        xdg_config(env, home) / "devin" / "mcp_config.json",
        home / ".codeium" / "windsurf" / "mcp_config.json",
        home / ".codeium" / "mcp_config.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return next((path for path in candidates if path.parent.is_dir()), candidates[0])


CLINE_LEGACY = ("globalStorage", "saoudrizwan.claude-dev")  # before Cline 4.0, which reads it once and moves on
ROO_STORAGE = ("globalStorage", "rooveterinaryinc.roo-cline")

CLIENTS: dict[str, Client] = {
    client.key: client
    for client in (
        Client(
            "gigacode",
            "GigaCode CLI",
            MCP_SERVERS,
            _home(".gigacode", "settings.json"),
            (_home(".gigacode"),),
            timeout=True,
        ),
        Client("qwen", "Qwen Code", MCP_SERVERS, _home(".qwen", "settings.json"), (_home(".qwen"),), timeout=True),
        Client("vscode", "VS Code", VSCODE, _vscode("mcp.json"), (_vscode(),)),
        Client(
            "cursor", "Cursor", MCP_SERVERS, _home(".cursor", "mcp.json"), (_home(".cursor"),), extra={"type": "stdio"}
        ),
        Client(
            "windsurf",
            "Windsurf (Devin Desktop)",
            MCP_SERVERS,
            windsurf_config,
            (_xdg("devin"), _home(".codeium", "windsurf")),
        ),
        Client(
            "cline",
            "Cline",
            MCP_SERVERS,
            _home(".cline", "data", "settings", "cline_mcp_settings.json"),
            (_home(".cline"), _vscode(*CLINE_LEGACY)),
            extra={"disabled": False},
        ),
        Client(
            "roo",
            "Roo Code",
            MCP_SERVERS,
            _vscode(*ROO_STORAGE, "settings", "mcp_settings.json"),
            (_vscode(*ROO_STORAGE),),
            extra={"disabled": False},
        ),
        Client("continue", "Continue", CONTINUE, _home(".continue", "mcpServers"), (_home(".continue"),)),
        Client("lmstudio", "LM Studio", MCP_SERVERS, _home(".lmstudio", "mcp.json"), (_home(".lmstudio"),)),
        Client(
            "claude-desktop",
            "Claude Desktop",
            MCP_SERVERS,
            _app("Claude", "claude_desktop_config.json"),
            (_app("Claude"),),
        ),
        Client("junie", "Junie", MCP_SERVERS, _home(".junie", "mcp", "mcp.json"), (_home(".junie"),), timeout=True),
        Client("jetbrains", "JetBrains AI Assistant", PASTE, None, (_app("JetBrains"),)),
    )
}


def server(client: Client, entry: Mapping[str, Any]) -> dict[str, Any]:
    """The entry as this client wants it: the same command, args and env, its own extra fields."""
    result = {"command": entry["command"], "args": list(entry["args"]), "env": dict(entry["env"])}
    if client.timeout and "timeout" in entry:
        result["timeout"] = entry["timeout"]
    if client.style == VSCODE:
        result = {"type": "stdio", **result}
    result.update(client.extra)
    return result


def snippet(client: Client, name: str, entry: Mapping[str, Any]) -> str:
    """What to add by hand: the whole wrapper with this one server in it."""
    one = server(client, entry)
    if client.style == CONTINUE:
        return continue_block(name, one)
    key = "servers" if client.style == VSCODE else MCP_SERVERS
    return json.dumps({key: {name: one}}, indent=2, ensure_ascii=False)


def continue_block(name: str, one: Mapping[str, Any]) -> str:
    """A Continue block file: YAML, written by hand so as not to need a YAML library.

    Every scalar is a JSON string, which is also a valid double-quoted YAML scalar.
    """
    lines = [
        f"name: {json.dumps(name)}",
        "version: 0.0.1",
        "schema: v1",
        "mcpServers:",
        f"  - name: {json.dumps(name)}",
        "    type: stdio",
    ]
    lines.append(f"    command: {json.dumps(one['command'], ensure_ascii=False)}")
    lines.append("    args:")
    lines.extend(f"      - {json.dumps(arg, ensure_ascii=False)}" for arg in one["args"])
    if one["env"]:
        lines.append("    env:")
        lines.extend(
            f"      {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}" for key, value in one["env"].items()
        )
    return "\n".join(lines) + "\n"


def write(client: Client, path: Path, name: str, entry: Mapping[str, Any]) -> Path | None:
    """Put the server into the client's file, keeping a dated copy of the old file; the copy's path, if any.

    A file that is not plain JSON (comments, say) is left alone: ClientConfigError carries the snippet.
    """
    one = server(client, entry)
    if client.style == CONTINUE:
        text = continue_block(name, one)
    else:
        key = "servers" if client.style == VSCODE else MCP_SERVERS
        settings = _read_json(path)
        if settings is None or not isinstance(settings.get(key, {}), dict):
            raise ClientConfigError(snippet(client, name, entry))
        settings.setdefault(key, {})[name] = one
        text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return backup


def _read_json(path: Path) -> dict[str, Any] | None:
    """The file's JSON object, {} for a missing or empty file, None for anything else."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except ValueError:
        return None
    return settings if isinstance(settings, dict) else None


def installed(env: Mapping[str, str], home: Path, *, besides: str | None = None) -> list[Client]:
    """The clients that seem to be installed here, in the order of CLIENTS, without the one named."""
    return [client for client in CLIENTS.values() if client.key != besides and client.present(env, home)]
