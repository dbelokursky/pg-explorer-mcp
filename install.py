#!/usr/bin/env python3
"""Register pg-explorer-mcp in GigaCode CLI (or Qwen Code) and check that it starts.

    python3 install.py            register in ~/.gigacode/settings.json, then check
    python3 install.py --check    only start the server and call status
    python3 install.py --client qwen
    python3 install.py --client cursor --print   show the entry for Cursor and where it goes
    python3 install.py --lang ru  the server and this script speak Russian
    PGDATABASE=sales python3 install.py --name sales  another database, as pg-sales
    python3 install.py import dbeaver            show what DBeaver's connections would bring
    python3 install.py import dbeaver --write    write the stands registry, the password file, register pg-stands
    python3 install.py import datagrip           show what DataGrip and the other JetBrains IDEs would bring
    python3 install.py import pg-service ~/.pg_service.conf --write
    python3 install.py export dbeaver            show which stand connections DBeaver would get
    python3 install.py export pgadmin --write    write servers.json for pgAdmin to import

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
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from pg_explorer_mcp import clients, environment, exports, i18n, imports
from pg_explorer_mcp.i18n import t

MIN_PYTHON = (3, 9)
ROOT = Path(__file__).resolve().parent
RUN_PY = str(ROOT / "run.py")
SERVER_NAME = "pg"  # further databases register as pg-<name>
STANDS_NAME = "stands"  # the entry an import registers: pg-stands, with PG_EXPLORER_MCP_STANDS=*
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,29}")
SETTINGS_FILES = {"gigacode": "~/.gigacode/settings.json", "qwen": "~/.qwen/settings.json"}
EXTENSION_CLIENTS = ("gigacode", "qwen")  # the clients that link pg-explorer-mcp as an extension too
COPIED_ENV = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGSSLMODE",
    "PGSSLROOTCERT",
    "PGPASSFILE",
    "PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS",
    "PG_EXPLORER_MCP_MAX_RESPONSE_CHARS",
    "PG_EXPLORER_MCP_PROFILE",
    "PG_EXPLORER_MCP_QUERY_LOG",
    "PG_EXPLORER_MCP_SOCKET_TIMEOUT_S",
    # many stands in one entry (ADR 0006)
    "PG_EXPLORER_MCP_STANDS",
    "PG_EXPLORER_MCP_STANDS_FILE",
    "PGSERVICEFILE",
    "PG_EXPLORER_MCP_MAX_CONNECTIONS",
)
PROFILES = ("schema", "explore", "analyst")
YES = ("y", "yes", "д", "да")


def shell_env() -> dict[str, str]:
    """This shell's variables, with the PG_MCP_* names of pg-mcp 0.7.0 read as their new twins."""
    return environment.with_legacy_names(os.environ)


def server_name(name: str | None = None) -> str:
    """The entry in mcpServers: pg, or pg-<name> for a further database."""
    return f"{SERVER_NAME}-{name}" if name else SERVER_NAME


def server_entry(profile: str | None = None, language: str | None = None, name: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": sys.executable, "args": ["-I", RUN_PY], "timeout": 30000}
    shell = shell_env()
    env = {variable: shell[variable] for variable in COPIED_ENV if shell.get(variable)}
    env["PG_EXPLORER_MCP_NAME"] = server_name(name)  # status says which entry, and so which database, it is
    if profile:
        env["PG_EXPLORER_MCP_PROFILE"] = profile
    if language:
        env["PG_EXPLORER_MCP_LANG"] = language
    entry["env"] = env
    return entry


def register(settings_path: str | Path, entry: dict[str, Any], name: str = SERVER_NAME) -> None:
    path = Path(settings_path)
    settings: Any = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            settings = json.loads(text) if text.strip() else {}
        except ValueError:
            settings = None
        if not isinstance(settings, dict) or not isinstance(settings.get("mcpServers", {}), dict):
            snippet = json.dumps({"mcpServers": {name: entry}}, indent=2, ensure_ascii=False)
            sys.exit(t("install.not_json", path=path, snippet=snippet))
        backup = path.with_name(f"{path.name}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        print(t("install.backup", path=backup))
    settings.setdefault("mcpServers", {})[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(t("install.registered", name=name, path=path))


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
                "clientInfo": {"name": "pg-explorer-mcp install.py"},
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
    return ready(text)


def ready(status: str) -> bool:
    """Whether the status text reports a connection, in whichever language the server speaks.

    An entry for many stands connects to none until asked about one, and the stands may be out of
    reach (behind a VPN, say): for it, finding stands in the registry is enough.
    """
    keys = ("status.connected", "status.stands")
    markers = tuple(i18n.CATALOG[language][key].partition("{")[0] for language in i18n.LANGUAGES for key in keys)
    return any(line.startswith(markers) for line in status.splitlines())


def _name(value: str) -> str:
    if not NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(t("install.bad_name", name=value))
    return value


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # Through a pipe, as an agent runs this script, Python writes in the system code page,
            # which may lack Cyrillic (cp1252) or the dash (cp866): print "?" rather than crash.
            stream.reconfigure(errors="replace")
    # The help texts are in the chosen language, so --lang is read before the parser is built.
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--lang", choices=i18n.LANGUAGES)
    language = early.parse_known_args(argv)[0].lang or i18n.language_from_env(shell_env(), i18n.system_locale())[0]
    i18n.use(language)
    if sys.version_info < MIN_PYTHON:
        sys.exit(t("install.old_python", need=".".join(map(str, MIN_PYTHON)), have=platform.python_version()))

    parser = argparse.ArgumentParser(description=t("install.description"))
    _common_options(parser)
    parser.add_argument("--check", action="store_true", help=t("install.help.check"))
    parser.add_argument("--name", type=_name, help=t("install.help.name"))
    commands = parser.add_subparsers(dest="command")
    importer = commands.add_parser("import", help=t("install.help.import"), description=t("install.help.import"))
    _common_options(importer)
    importer.add_argument("source", choices=("dbeaver", "datagrip", "pg-service"), help=t("install.help.source"))
    importer.add_argument("file", nargs="?", help=t("install.help.file"))
    importer.add_argument("--workspace", help=t("install.help.workspace"))
    importer.add_argument("--project", help=t("install.help.project"))
    importer.add_argument("--write", action="store_true", help=t("install.help.write"))
    importer.add_argument("--prune", action="store_true", help=t("install.help.prune"))
    importer.add_argument(
        "--stand-pattern",
        default=imports.DEFAULT_STAND_PATTERN,
        help=t("install.help.stand_pattern", pattern=imports.DEFAULT_STAND_PATTERN),
    )
    exporter = commands.add_parser("export", help=t("export.help"), description=t("export.help"))
    exporter.add_argument("tool", choices=list(exports.TOOLS), help=t("export.help.tool"))
    exporter.add_argument("--stands", default="*", help=t("export.help.stands"))
    exporter.add_argument("--write", action="store_true", help=t("export.help.write"))
    exporter.add_argument("--workspace", help=t("install.help.workspace"))
    exporter.add_argument("--project", help=t("export.help.project", path=exports.default_project(shell_env())))
    exporter.add_argument("--editor", choices=list(exports.EDITORS), default="vscode", help=t("export.help.editor"))
    exporter.add_argument("--lang", choices=i18n.LANGUAGES, help=t("install.help.lang"))
    args = parser.parse_args(argv)
    if args.command == "import":
        return import_command(args, language)
    if args.command == "export":
        return export_command(args)

    entry = server_entry(args.profile, language, args.name)
    name = server_name(args.name)
    if args.print:
        show(clients.CLIENTS[args.client], name, entry)
        return 0
    if not args.check:
        install_into(args.client, name, entry)
    ok = check(entry)
    if not args.check:
        print()
        if args.client in EXTENSION_CLIENTS:
            link_hints(args.client)
        print(t("install.restart"))
        offer_clients(name, entry, besides=args.client)
        if args.name is None:
            offer_import(language)
        offer_export()  # pg-stands is registered with --name stands
    return 0 if ok else 1


def link_hints(client: str) -> None:
    """How to link the extension, and which folder an extension linked as pg-mcp left in the client."""
    print(t("install.next", client=client, path=ROOT))
    old = old_extension(client)
    if old is not None:
        print(t("install.old_extension", path=old))


def old_extension(client: str) -> Path | None:
    """The client's folder of the extension linked as pg-mcp before 0.8.0, when there is one (ADR 0007).

    The client loads it under the new name from the manifest, but cannot remove it by either name, and
    refuses a new link while it is there. Its format is the client's, so install.py only names it.
    """
    folder = Path(SETTINGS_FILES[client]).expanduser().parent / "extensions" / "pg-mcp"
    return folder if folder.is_dir() else None


def install_into(key: str, name: str, entry: dict[str, Any]) -> bool:
    """Register the entry with one client: its file, or the text to paste for a client without one."""
    client = clients.CLIENTS[key]
    if key in SETTINGS_FILES:
        register(Path(SETTINGS_FILES[key]).expanduser(), entry, name)
        return True
    path = client.path(shell_env(), Path.home(), name)
    if path is None:
        show(client, name, entry)
        return True
    try:
        backup = clients.write(client, path, name, entry)
    except clients.ClientConfigError as exc:
        print(t("install.not_json", path=path, snippet=exc))
        return False
    if backup is not None:
        print(t("install.backup", path=backup))
    print(t("install.registered", name=name, path=path))
    return True


def show(client: clients.Client, name: str, entry: dict[str, Any]) -> None:
    """Print the entry in the client's format and where it goes; nothing is written."""
    path = client.path(shell_env(), Path.home(), name)
    if path is None:
        print(t("clients.where_paste", client=client.label))
    else:
        print(t("clients.where_file", client=client.label, path=path))
    print(clients.snippet(client, name, entry))


def offer_clients(name: str, entry: dict[str, Any], *, besides: str) -> None:
    """At a terminal: offer to register the entry with the other clients installed on this machine."""
    found = clients.installed(shell_env(), Path.home(), besides=besides)
    if not found or not sys.stdin.isatty():
        return
    labels = ", ".join(client.label for client in found)
    try:
        answer = input(t("clients.offer", clients=labels, name=name))
    except EOFError:
        return
    if answer.strip().lower() in YES:
        for client in found:
            print()
            install_into(client.key, name, entry)
        return
    keys = "|".join(client.key for client in found)
    print(t("clients.offer_hint", name=name, command=f"{sys.executable} {ROOT / 'install.py'} --client {keys}"))


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--client", choices=list(clients.CLIENTS), default="gigacode", help=t("install.help.client"))
    parser.add_argument("--print", action="store_true", help=t("install.help.print"))
    parser.add_argument("--profile", choices=PROFILES, help=t("install.help.profile"))
    parser.add_argument("--lang", choices=i18n.LANGUAGES, help=t("install.help.lang"))


def import_command(args: argparse.Namespace, language: str) -> int:
    """Preview, or with --write bring, the connections of DBeaver, DataGrip or pg_service.conf into pg-explorer-mcp."""
    try:
        connections = _connections(args)
        pattern = imports.compile_pattern(args.stand_pattern)
    except imports.ImportRefusedError as exc:
        print(exc)
        return 1
    if connections is None:
        return 2
    registry_file, passfile = imports.default_files(shell_env())
    result = imports.plan(connections, pattern)
    for line in imports.preview(result, registry_file, passfile):
        print(line)
    if not result.targets:
        print(t("import.nothing"))
        return 1
    if not args.write:
        print(t("import.dry_run"))
        return 0
    try:
        written = imports.write(result, registry_file, passfile, prune=args.prune)
    except imports.ImportRefusedError as exc:
        print(exc)
        return 1
    for line in written:
        print(line)
    entry = server_entry(args.profile, language, STANDS_NAME)
    entry["env"]["PG_EXPLORER_MCP_STANDS"] = "*"
    install_into(args.client, server_name(STANDS_NAME), entry)
    ok = check(entry)
    print()
    print(t("install.restart"))
    offer_clients(server_name(STANDS_NAME), entry, besides=args.client)
    offer_export(besides=args.source)  # the stands are in the tool they came from already
    return 0 if ok else 1


def _connections(args: argparse.Namespace) -> list[imports.Connection] | None:
    if args.source == "dbeaver":
        folder = Path(args.workspace).expanduser() if args.workspace else imports.dbeaver_workspace(shell_env())
        if folder is None:
            print(t("import.no_dbeaver"))
            return None
        print(t("import.reading", source=folder))
        return imports.read_dbeaver(folder)
    if args.source == "datagrip":
        return _datagrip_connections(_folder(args.project))
    if not args.file:
        print(t("import.need_file"))
        return None
    path = Path(args.file).expanduser()
    print(t("import.reading", source=path))
    return imports.read_pg_service(path)


def _datagrip_connections(project: Path | None) -> list[imports.Connection] | None:
    """The data sources of every JetBrains folder found; a folder that cannot be read is named and skipped."""
    folders = imports.datagrip_folders(shell_env(), Path.home(), project)
    if not folders:
        print(t("import.no_datagrip"))
        return None
    connections: list[imports.Connection] = []
    ssh_configs = imports.jetbrains_ssh_configs(shell_env(), Path.home())  # those the IDEs share between projects
    for folder in folders:
        print(t("import.reading", source=folder))
        try:
            connections.extend(imports.read_datagrip(folder, ssh_configs))
        except imports.ImportRefusedError as exc:
            print(exc)  # one broken project or IDE leaves the others to read
    return connections


def offer_import(language: str) -> None:
    """When DBeaver is on this machine: at a terminal, offer to show what an import would do; otherwise name
    the command that shows it, for the agent that runs install.py with no terminal to answer (/pg:setup)."""
    folder = imports.dbeaver_workspace(shell_env())
    if folder is None:
        return
    if not sys.stdin.isatty():
        preview = f"{sys.executable} {ROOT / 'install.py'} import dbeaver --lang {language}"
        print(t("import.found", path=folder, command=preview))
        return
    try:
        answer = input(t("import.offer", path=folder))
    except EOFError:
        return
    command = f"{sys.executable} {ROOT / 'install.py'} import dbeaver --write --lang {language}"
    if answer.strip().lower() in YES:
        print()
        import_command(argparse.Namespace(source="dbeaver", file=None, workspace=None, write=False, prune=False,
                                          stand_pattern=imports.DEFAULT_STAND_PATTERN, client="gigacode",
                                          profile=None, lang=language, print=False), language)  # fmt: skip
    print(t("import.offer_hint", command=command))


def export_command(args: argparse.Namespace) -> int:
    """Preview, or with --write make, the stand connections in a database tool."""
    try:
        items, warnings = exports.connections(shell_env(), args.stands.split(","))
        export = exports.prepare(
            args.tool,
            items,
            shell_env(),
            Path.home(),
            workspace=_folder(args.workspace),
            project=_folder(args.project),
            editor=args.editor,
        )
    except exports.ExportRefusedError as exc:
        print(exc)
        return 1
    for line in [*warnings, *exports.preview(export)]:
        print(line)
    if not export.files:
        return 0
    if not args.write:
        print(t("export.dry_run"))
        return 0
    return _write_export(export)


def _write_export(export: exports.Export) -> int:
    try:
        lines = exports.write(export)
    except exports.ExportRefusedError as exc:
        print(exc)
        return 1
    for line in lines:
        print(line)
    return 0


def _folder(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def offer_export(*, besides: str | None = None) -> None:
    """At a terminal, when stands are registered and database tools are here: offer to add the stands to them.

    besides is the tool the stands were just imported from. A tool that has every connection already,
    or whose settings cannot be changed safely, is not offered; the export command says why.
    """
    if not sys.stdin.isatty():
        return
    try:
        items, _ = exports.connections(shell_env())
    except exports.ExportRefusedError:
        return
    ready: list[tuple[exports.Export, str]] = []
    for tool, editor in exports.installed(shell_env(), Path.home()):
        if tool == besides:
            continue
        try:
            export = exports.prepare(tool, items, shell_env(), Path.home(), editor=editor or "vscode")
        except exports.ExportRefusedError:
            continue
        if export.files:
            ready.append((export, editor))
    if not ready:
        return
    tools = ", ".join(exports.TOOLS[export.tool] for export, _ in ready)
    try:
        answer = input(t("export.offer", tools=tools, count=len(items)))
    except EOFError:
        return
    if answer.strip().lower() in YES:
        for export, editor in ready:
            print()
            _export_now(export.tool, items, editor)
        return
    print(t("export.offer_hint"))
    for export, editor in ready:
        option = f" --editor {editor}" if editor and editor != "vscode" else ""
        print(f"  {sys.executable} {ROOT / 'install.py'} export {export.tool}{option} --write")


def _export_now(tool: str, items: list[exports.Connection], editor: str) -> None:
    """Prepare one export and write it. It is prepared only now: both VS Code extensions change one file."""
    try:
        export = exports.prepare(tool, items, shell_env(), Path.home(), editor=editor or "vscode")
    except exports.ExportRefusedError as exc:
        print(exc)
        return
    for line in exports.preview(export):
        print(line)
    if export.files:
        _write_export(export)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
