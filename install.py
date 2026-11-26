#!/usr/bin/env python3
"""Register pg-explorer-mcp in GigaCode CLI (or Qwen Code) and check that it starts.

    python3 install.py            register in ~/.gigacode/settings.json, then check
    python3 install.py --check    only start the server and call status
    python3 install.py --client qwen
    python3 install.py --client cursor --print   show the entry for Cursor and where it goes
    python3 install.py --lang ru  the server and this script speak Russian
    PGDATABASE=sales python3 install.py --name sales  another database, as pg-sales
    python3 install.py import dbeaver            show what DBeaver's connections would bring
    python3 install.py import dbeaver --write    write the stands registry, the passwords into the password store
    python3 install.py import datagrip           show what DataGrip and the other JetBrains IDEs would bring
    python3 install.py import pg-service ~/.pg_service.conf --write
    python3 install.py export dbeaver            show which stand connections DBeaver would get
    python3 install.py export pgadmin --write    write servers.json for pgAdmin to import
    python3 install.py export pg-service --write --pgpass   and the passwords into ~/.pgpass, in clear, for psql
    python3 install.py password                  put the password of this database into the password store
    python3 install.py password --stand dev7 --schema billing
    python3 install.py password --move           carry the password over from ~/.pgpass

Uses only the standard library and registers the very interpreter that runs it,
so the agent later starts the server on a Python that is known to work. PG*
variables set in the current shell, except PGPASSWORD, are copied into the
server entry; the password belongs in the password store of the system, which
`install.py password` writes (ADR 0012), never in the settings and never in the chat.
The language goes into the entry as well: an agent may start the server
without the locale variables of this shell.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from pg_explorer_mcp import clients, environment, exports, i18n, imports, passwords, user_files
from pg_explorer_mcp.db import split_hosts
from pg_explorer_mcp.i18n import t
from pg_explorer_mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS
from pg_explorer_mcp.stands import StandError, load_registry, registry_files
from pg_explorer_mcp.user_files import place, stage

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
    "PG_EXPLORER_MCP_GLOSSARY_FILE",
    "PG_EXPLORER_MCP_QUERIES_FILE",
    "PG_EXPLORER_MCP_SOCKET_TIMEOUT_S",
    # many stands in one entry (ADR 0006)
    "PG_EXPLORER_MCP_STANDS",
    "PG_EXPLORER_MCP_STANDS_FILE",
    "PGSERVICEFILE",
    "PG_EXPLORER_MCP_MAX_CONNECTIONS",
    # the password store of the system (ADR 0012); off is the value worth passing on
    passwords.STORE_VARIABLE,
)
PROFILES = ("schema", "explore", "analyst")
YES = ("y", "yes", "д", "да")


def shell_env() -> dict[str, str]:
    """The shell's variables, with the PG_MCP_* names of pg-mcp 0.7.0 read as their new twins."""
    return environment.with_legacy_names(os.environ)


def server_name(name: str | None = None) -> str:
    """The entry in mcpServers: pg, or pg-<name> for a further database."""
    return f"{SERVER_NAME}-{name}" if name else SERVER_NAME


def server_entry(profile: str | None = None, language: str | None = None, name: str | None = None) -> dict[str, Any]:
    """The pg entry for the agent settings, as the agent will start it."""
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
    """Write the entry into the settings file, under its name, keeping a dated copy of the old file (user_files)."""
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
    settings.setdefault("mcpServers", {})[name] = entry
    backup = place(stage(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n"))
    if backup is not None:
        print(t("install.backup", path=backup))
    print(t("install.registered", name=name, path=path))


def check(entry: dict[str, Any]) -> bool:
    """Start the server the way the agent will and call its status tool."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                # The newest revision the server speaks: the check exercises the protocol the
                # newest clients use, and there is one place now for the version to live.
                "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
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
    """Everything install.py does, from arguments to the check at the end."""
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
    _commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    if args.command == "import":
        return import_command(args, language)
    if args.command == "export":
        return export_command(args)
    if args.command == "password":
        return password_command(args)

    entry = server_entry(args.profile, language, args.name)
    name = server_name(args.name)
    if args.print:
        show(clients.CLIENTS[args.client], name, entry)
        return 0
    if not args.check and not install_into(args.client, name, entry):
        # Why it could not register was said: the agent's settings could not be written, or another
        # command is writing them. There is nothing registered to check the reach of.
        return 1
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
    """Register the entry with one client: its file, or the text to paste for a client without one.

    The file is read, merged and written under the lock of user_files, as every writing is: the agent
    rewrites its settings itself, and another command of ours may.
    """
    with _writing() as got_in:
        if not got_in:
            return False  # another command is writing: said by _writing, and nothing was registered
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


def _commands(commands: Any) -> None:
    """The subcommands: import, export and password."""
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
    exporter.add_argument("--pgpass", action="store_true", help=t("export.help.pgpass"))
    exporter.add_argument("--workspace", help=t("install.help.workspace"))
    exporter.add_argument("--project", help=t("export.help.project", path=exports.default_project(shell_env())))
    exporter.add_argument("--editor", choices=list(exports.EDITORS), default="vscode", help=t("export.help.editor"))
    exporter.add_argument("--lang", choices=i18n.LANGUAGES, help=t("install.help.lang"))
    secret = commands.add_parser("password", help=t("install.help.password"), description=t("install.help.password"))
    secret.add_argument("--host", help=t("install.help.host"))
    secret.add_argument("--port", help=t("install.help.port"))
    secret.add_argument("--database", help=t("install.help.database"))
    secret.add_argument("--user", help=t("install.help.user"))
    secret.add_argument("--stand", help=t("install.help.stand"))
    secret.add_argument("--schema", help=t("install.help.schema"))
    secret.add_argument("--move", action="store_true", help=t("install.help.move"))


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
    registry_file = registry_files(shell_env())[0]
    store = passwords.current(shell_env())  # None where the system has no password store (ADR 0012)
    result = imports.plan(connections, pattern)
    for line in imports.preview(result, registry_file, store):
        print(line)
    if not result.targets:
        print(t("import.nothing"))
        return 1
    if not args.write:
        print(t("import.dry_run"))
        return 0
    written: list[str] = []
    try:
        with _writing() as got_in:
            if got_in:
                written = imports.write(result, registry_file, store, prune=args.prune)
    except imports.ImportRefusedError as exc:
        print(exc)
        return 1
    if not got_in:
        return 1  # another command is writing, and _writing said so
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


def password_command(args: argparse.Namespace) -> int:
    """Put a password into the password store (ADR 0012): typed here, or carried over from the password file."""
    env = shell_env()
    try:
        logins = _logins(args, env)
    except (StandError, ValueError) as exc:
        print(exc)
        return 1
    if not logins:
        print(t("install.password_login"))
        return 2
    store = passwords.current(env)
    if store is None:
        print(t("install.no_store"))
        if sys.platform.startswith("linux"):  # the desktop keyring is there; its tool may not be
            print(t("install.secret_tool_missing"))
        return 1
    named = ", ".join(f"{user}@{host}:{port}/{database}" for host, port, database, user in logins)
    if args.move:
        with _writing() as got_in:
            return _move_password(env, store, logins, named) if got_in else 1
    if not sys.stdin.isatty():
        print(t("install.password_needs_terminal"))
        return 2
    typed = getpass.getpass(t("install.password_prompt", login=named))
    if typed != getpass.getpass(t("install.password_again")):
        print(t("install.password_differ"))
        return 1
    if not typed:
        print(t("install.password_empty"))
        return 1
    with _writing() as got_in:
        return _saved(store, dict.fromkeys(logins, typed), named) if got_in else 1


@contextlib.contextmanager
def _writing() -> Iterator[bool]:
    """A command's writing under the lock of user_files.exclusive: one command at a time reads, merges, writes.

    Two commands making the same cycle at once would each write from what they read before the other
    wrote, and one change would be lost. The block gets whether the command got in: False when the one
    before it held the lock for longer than it may be waited for. That was said, and the block writes nothing.
    """
    with contextlib.ExitStack() as lock:
        try:
            # The folder of our own files, next to the stands registry.
            lock.enter_context(
                user_files.exclusive(registry_files(shell_env())[0].parent, timeout=user_files.LOCK_TIMEOUT_S)
            )
        except user_files.StillLockedError:
            print(t("install.locked", seconds=user_files.LOCK_TIMEOUT_S))
            yield False
        else:
            yield True


def _saved(store: passwords.Passwords, found: dict[passwords.Login, str], named: str) -> int:
    """Put these passwords into the store; what went wrong is said without a traceback."""
    try:
        store.put(found)
    except passwords.PasswordStoreError as exc:
        print(exc)
        return 1
    print(t("install.password_saved", login=named, store=store.label))
    return 0


def _move_password(env: dict[str, str], store: passwords.Passwords, logins: list[passwords.Login], named: str) -> int:
    """Carry the password of these logins over from the password file into the store, and drop its lines there.

    A line with * in it stays: it may be the password of other logins too, and from now on the store wins.
    """
    passfile = passwords.pgpass_path(env)
    found: dict[passwords.Login, str] = {}
    for login in logins:
        password = passwords.read_pgpass(passfile, login[0], int(login[1]), login[2], login[3])
        if password is not None:
            found[login] = password
    if not found:
        print(t("install.nothing_to_move", login=named))
        return 1
    if _saved(store, found, named):
        return 1
    dropped = _drop_lines(Path(passfile), logins)
    if dropped is not None:
        lines, backup = dropped
        print(t("install.password_moved", count=lines, backup=backup))
    return 0


def _drop_lines(passfile: Path, logins: list[passwords.Login]) -> tuple[int, Path | None] | None:
    """Rewrite the password file without the lines of exactly these logins; how many went, and the dated copy."""
    try:
        lines = passfile.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    kept = passwords.without(lines, logins)
    if len(kept) == len(lines):
        return None
    backup = place(stage(passfile, "\n".join(kept) + ("\n" if kept else "")), owner_only=True)
    return len(lines) - len(kept), backup


def _logins(args: argparse.Namespace, env: dict[str, str]) -> list[passwords.Login]:
    """The logins the password is for: those of a stand's schema, or of the database the options and PG* name.

    Empty when nothing names a database; StandError when the stand or the schema is not in the registry.
    """
    if args.stand:
        # A stand was named: the registry is read whatever PG_EXPLORER_MCP_STANDS says, which is the agent's
        # setting and need not be in the shell of the user who types this command.
        registry = load_registry(env, patterns=["*"])
        if registry is None:
            raise StandError(t("stands.unknown", stand=args.stand, stands=t("error.none")))
        target = registry.target(registry.stand(args.stand), args.schema, args.database)
        database = target.options.get("dbname", target.database)  # what the server of that section connects to
        return [(host, str(port), database, target.login) for host, port in target.endpoints()]
    user = args.user or env.get("PGUSER") or getpass.getuser()
    database = args.database or env.get("PGDATABASE") or user
    if not (args.host or env.get("PGHOST")):
        return []
    port = args.port or env.get("PGPORT") or "5432"
    return [(host, str(number), database, user) for host, number in split_hosts(args.host or env["PGHOST"], port)]


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
    """Offer the import preview when DBeaver or a JetBrains IDE has connections on this machine.

    At a terminal, the offer is to show what an import would do; otherwise the command that shows
    it is named, for the agent that runs install.py with no terminal to answer (/pg:setup). Both
    found make one question, not two in a row.
    """
    found: dict[str, str] = {}  # the source to import from, and where its connections were found
    dbeaver = imports.dbeaver_workspace(shell_env())
    if dbeaver is not None:
        found["dbeaver"] = t("import.found_dbeaver", path=dbeaver)
    jetbrains = imports.datagrip_folders(shell_env(), Path.home())
    if jetbrains:
        found["datagrip"] = t("import.found_datagrip", paths=", ".join(map(str, jetbrains)))
    if not found:
        return
    command = f"{sys.executable} {ROOT / 'install.py'} import"
    if not sys.stdin.isatty():
        for source, where in found.items():
            print(t("import.found", where=where, command=f"{command} {source} --lang {language}"))
        return
    try:
        answer = input(t("import.offer", where=" ".join(found.values())))
    except EOFError:
        return
    if answer.strip().lower() in YES:
        for source in found:
            print()
            import_command(argparse.Namespace(source=source, file=None, workspace=None, project=None, write=False,
                                              prune=False, stand_pattern=imports.DEFAULT_STAND_PATTERN,
                                              client="gigacode", profile=None, lang=language, print=False),
                           language)  # fmt: skip
    print(t("import.offer_hint"))
    for source in found:
        print(f"  {command} {source} --write --lang {language}")


def export_command(args: argparse.Namespace) -> int:
    """Preview, or with --write make, the stand connections in a database tool."""
    try:
        if args.write:
            with _writing() as got_in:
                return _export_of(args)() if got_in else 1  # read and merged under the lock it is written under
        return _export_of(args)()
    except exports.ExportRefusedError as exc:
        print(exc)
        return 1


def _export_of(args: argparse.Namespace) -> Callable[[], int]:
    """The export of the command: its preview, and with --write its files; refused before anything is written."""

    def export() -> int:
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
        for line in [*warnings, *exports.preview(export)]:
            print(line)
        if not export.files:
            return 0
        if not args.write:
            print(t("export.dry_run"))
            return 0
        return _write_export(export) or (_write_pgpass(items) if args.pgpass else 0)

    return export


def _write_pgpass(items: list[exports.Connection]) -> int:
    """The passwords of these connections, out of the password store and into the libpq password file.

    In clear, for psql, which reads no other store; only because --pgpass asked for it (ADR 0012).

    A line of another login stays; a line of the same login is replaced; the old file is a dated copy.
    """
    env = shell_env()
    store = passwords.current(env)
    if store is None:
        print(t("install.no_store"))
        return 1
    found: dict[passwords.Login, str] = {}
    missing = 0
    for item in items:
        for host, port in item.hosts:
            password = store.lookup(host, port, item.dbname, item.user)
            if password is None:
                missing += 1
            else:
                found[(host, str(port), item.dbname, item.user)] = password
    passfile = Path(passwords.pgpass_path(env))
    if not found:
        print(t("export.pgpass_nothing", path=passfile, missing=missing))
        return 1
    try:
        existing = passfile.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, NotADirectoryError):
        existing = []  # no password file yet: the exported logins start one
    except (OSError, UnicodeDecodeError) as exc:
        # Counted as empty, every other password of the user would be gone from the new file.
        print(t("import.unreadable", path=passfile, error=exc))
        return 1
    lines = passwords.merged(existing, found)
    try:
        backup = place(stage(passfile, "\n".join(lines) + "\n"), owner_only=True)
    except OSError as exc:
        print(t("import.cannot_place", path=passfile, error=exc))
        return 1
    print(t("export.pgpass_written", path=passfile, count=len(found), missing=missing))
    if backup is not None:
        print(t("import.written_backup", path=passfile, backup=backup))
    return 0


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
    """Prepare one export and write it, under the lock as every writing is.

    It is prepared only now: both VS Code extensions change one file.
    """
    with _writing() as got_in:
        if got_in:
            _export_written(tool, items, editor)


def _export_written(tool: str, items: list[exports.Connection], editor: str) -> int:
    try:
        export = exports.prepare(tool, items, shell_env(), Path.home(), editor=editor or "vscode")
    except exports.ExportRefusedError as exc:
        print(exc)
        return 1
    for line in exports.preview(export):
        print(line)
    if export.files:
        return _write_export(export)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
