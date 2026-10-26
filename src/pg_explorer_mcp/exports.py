"""Export of the stand connections to database tools: DBeaver, JetBrains, pgAdmin, libpq, VS Code extensions.

The reverse of imports.py: the stands registry (ADR 0006) becomes connections in the tools people
use next to the agent. No password is copied anywhere: the connections log in with the libpq
password file, which DBeaver, DataGrip, pgAdmin, psql and SQLTools all read; only the PostgreSQL
extension of VS Code cannot, and asks once. Each tool gets a file of its own where it allows one,
and an export replaces only the connections it brings: what the user made in a tool stays, and so
do the connections an earlier export brought for other stands.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from .clients import app_data
from .db import pgpass_path
from .i18n import plural, t
from .imports import ImportRefusedError, dbeaver_workspace, read_dbeaver
from .stands import EXPORT_MARKS, EXPORTED, Target, load_registry, registry_files, summarize

NAMESPACE = uuid.UUID("6f0f7a4e-2b43-4d0b-8a5e-3d1f2a9c7e10")  # stable ids: a re-export updates, never duplicates
# This file and the ids of the connections keep the name pg-mcp: DBeaver and VS Code tie a user's own
# settings of a connection to them, and a new name would make the next export start over (ADR 0007).
DBEAVER_FILE = "data-sources-pg-mcp.json"  # DBeaver loads every data-sources*.json of a project


class ExportRefusedError(Exception):
    """The export cannot be done; the message says why. Nothing was written."""


@dataclass(frozen=True)
class Connection:
    """One stand connection with everything a tool needs spelled out: no defaults left to the reader."""

    stand: str
    database: str  # the registry's name for it, which the label shows
    schema: str
    host: str
    port: int
    dbname: str
    user: str
    sslmode: str | None
    sslrootcert: str | None = None  # the CA certificate file, for a stand whose server certificate is checked
    source: str = field(default="", compare=False)  # the registry file the connection comes from

    @property
    def label(self) -> str:
        return f"{self.stand} / {self.database} / {self.schema}"

    @property
    def key(self) -> str:
        return f"{self.stand}.{self.database}.{self.schema}"

    def uid(self) -> str:
        return str(uuid.uuid5(NAMESPACE, self.key))


def connections(env: Mapping[str, str], patterns: Sequence[str] = ("*",)) -> tuple[list[Connection], list[str]]:
    """The registry's connections of the stands the patterns name, in the registry's order, and its warnings."""
    wanted = [pattern.strip().lower() for pattern in patterns if pattern.strip()] or ["*"]
    registry = load_registry(env, patterns=wanted)
    if registry is not None and registry.targets:
        return [connection(target) for target in registry.targets], registry.warnings
    everything = load_registry(env, patterns=["*"])
    if everything is not None and everything.targets:
        stands = summarize(everything.stands())
        raise ExportRefusedError(t("export.no_match", patterns=", ".join(wanted), stands=stands))
    raise ExportRefusedError(t("export.no_stands"))


def connection(target: Target) -> Connection:
    options = target.options
    return Connection(
        stand=target.stand,
        database=target.database,
        schema=target.schema,
        host=options["host"],
        port=int(options.get("port", "5432")),
        dbname=options.get("dbname", target.database),
        user=options.get("user", target.schema),
        sslmode=options.get("sslmode") or None,
        sslrootcert=options.get("sslrootcert") or None,
        source=target.source,
    )


def tls_settings(item: Connection) -> dict[str, str]:
    """sslmode and sslrootcert where the registry sets them: libpq and pgjdbc both take these names."""
    settings = {"sslmode": item.sslmode, "sslrootcert": item.sslrootcert}
    return {key: value for key, value in settings.items() if value}


def jdbc_url(item: Connection, *, current_schema: bool = False, tls: bool = True) -> str:
    """The pgjdbc URL; with current_schema, the schema comes first on the search path, as on the server.

    Without tls the URL leaves the TLS settings to the driver properties.
    """
    parameters = []
    if current_schema:
        parameters.append(f"currentSchema={item.schema},public")
    if tls:  # pgjdbc decodes the values: a path may hold spaces or backslashes
        parameters.extend(f"{key}={quote(value, safe='/:')}" for key, value in tls_settings(item).items())
    query = "?" + "&".join(parameters) if parameters else ""
    return f"jdbc:postgresql://{item.host}:{item.port}/{item.dbname}{query}"


# --- libpq: psql, pgcli and every other tool on libpq read ~/.pg_service.conf ------------------------


def pg_service(items: Sequence[Connection], existing: str) -> str:
    """The service file with these sections, each under the EXPORTED line; everything else stays as it was.

    A section an earlier export wrote for one of these stands is replaced; a section the user wrote is
    never touched, and prepare() leaves its stand out. A comment after the last setting of a replaced
    section belongs to what follows it, so it stays.
    """
    wanted = {item.key for item in items}
    lines = existing.splitlines()
    headers = [number for number, line in enumerate(lines) if _section(line) is not None]
    drop: set[int] = set()
    for position, start in enumerate(headers):
        if _section(lines[start]) in wanted and _exported(lines, start):
            end = headers[position + 1] if position + 1 < len(headers) else len(lines)
            last = max((number for number in range(start + 1, end) if _setting(lines[number])), default=start)
            first = start - 1  # the EXPORTED line, and the empty lines above it
            while first > 0 and not lines[first - 1].strip():
                first -= 1
            drop.update(range(first, last + 1))
    out = [line for number, line in enumerate(lines) if number not in drop]
    while out and not out[-1].strip():
        out.pop()
    for item in items:
        out.extend(["", EXPORTED, f"[{item.key}]", f"host={item.host}", f"port={item.port}"])
        out.extend([f"dbname={item.dbname}", f"user={item.user}"])
        out.extend(f"{key}={value}" for key, value in tls_settings(item).items())
        out.append(f"options=-c search_path={item.schema},public")  # as the server does: the schema first
    return "\n".join(out).lstrip("\n") + "\n"


def service_sections(text: str) -> dict[str, bool]:
    """The sections of a service file, each with whether an export wrote it; a name the user wrote is theirs."""
    lines = text.splitlines()
    found: dict[str, bool] = {}
    for number, line in enumerate(lines):
        name = _section(line)
        if name is not None:
            found[name] = found.get(name, True) and _exported(lines, number)
    return found


def _section(line: str) -> str | None:
    """The section a line opens, its stand in lower case as the registry has it; None for any other line."""
    stripped = line.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return None
    parts = stripped[1:-1].strip().split(".")
    if len(parts) == 3:  # stand.database.schema
        parts[0] = parts[0].lower()
    return ".".join(parts)


def _exported(lines: Sequence[str], header: int) -> bool:
    return header > 0 and lines[header - 1].strip().startswith(EXPORT_MARKS)


def _setting(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


# --- DBeaver ------------------------------------------------------------------------------------------


def dbeaver(items: Sequence[Connection], previous: Mapping[str, Any] | None = None) -> str:
    """pg-explorer-mcp's own data-sources file: a folder per stand, PgPass authentication, the schema as default.

    previous is the file as an earlier export left it: its connections stay unless these replace them.
    """
    old = previous or {}
    folders: dict[str, Any] = dict(old["folders"]) if isinstance(old.get("folders"), dict) else {}
    data_sources: dict[str, Any] = dict(old["connections"]) if isinstance(old.get("connections"), dict) else {}
    for item in items:
        folders.setdefault(item.stand, {})
        configuration: dict[str, Any] = {
            "host": item.host,
            "port": str(item.port),
            "database": item.dbname,
            "url": jdbc_url(item, tls=False),  # DBeaver builds the URL of a MANUAL connection itself
            "configurationType": "MANUAL",
            "type": "dev",
            "auth-model": "postgres_pgpass",
            "user": item.user,
            "bootstrap": {"defaultSchema": item.schema},
            "provider-properties": {"@dbeaver-show-non-default-db@": "false"},
        }
        if tls_settings(item):
            configuration["properties"] = tls_settings(item)  # driver properties: pgjdbc gets them as they are
        data_sources[f"postgres-jdbc-pg-mcp-{item.uid()}"] = {
            "provider": "postgresql",
            "driver": "postgres-jdbc",
            "name": item.label,
            "folder": item.stand,
            "save-password": False,
            "configuration": configuration,
        }
    data = {**old, "folders": folders, "connections": data_sources}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def in_dbeaver(folder: Path) -> set[tuple[str, int, str, str]]:
    """Host, port, database and user of the connections DBeaver has of its own; "" where the user is unknown.

    The user is unknown when DBeaver keeps the logins in its secure storage.
    """
    try:
        existing = read_dbeaver(folder)
    except ImportRefusedError:
        return set()
    return {
        (item.host.lower(), int(item.port) if item.port.isdigit() else 5432, item.database, item.user)
        for item in existing
    }


def not_in_dbeaver(items: Sequence[Connection], present: set[tuple[str, int, str, str]]) -> list[Connection]:
    """The connections DBeaver does not have of its own.

    A connection of DBeaver whose user is unknown stands for a login of the registry only where the
    registry has a single login to that database: with several, there is no telling which it is.
    """
    logins = Counter((item.host.lower(), item.port, item.dbname) for item in items)

    def known(item: Connection) -> bool:
        address = (item.host.lower(), item.port, item.dbname)
        return (*address, item.user) in present or ((*address, "") in present and logins[address] == 1)

    return [item for item in items if not known(item)]


# --- DataGrip and the IDEs of JetBrains with database tools -----------------------------------------


def datagrip(items: Sequence[Connection]) -> tuple[str, str]:
    """A project's .idea/dataSources.xml and .idea/dataSources.local.xml for these connections.

    The shared file holds the driver and URL; the local one the user name, pgpass authentication
    and the schema to introspect.
    """
    shared = ['<?xml version="1.0" encoding="UTF-8"?>', '<project version="4">']
    shared.append('  <component name="DataSourceManagerImpl" format="xml" multifile-model="true">')
    local = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<project version="4">',
        '  <component name="dataSourceStorageLocal">',
    ]
    for item in items:
        head = (
            f'<data-source source="LOCAL" name={quoteattr(item.label)} group={quoteattr(item.stand)}'
            f" uuid={quoteattr(item.uid())}>"
        )
        shared += [
            f"    {head}",
            "      <driver-ref>postgresql</driver-ref>",
            "      <synchronize>true</synchronize>",
            "      <jdbc-driver>org.postgresql.Driver</jdbc-driver>",
            f"      <jdbc-url>{escape(jdbc_url(item, current_schema=True))}</jdbc-url>",
            "      <working-dir>$ProjectFileDir$</working-dir>",
            "    </data-source>",
        ]
        local += [
            f"    <data-source name={quoteattr(item.label)} uuid={quoteattr(item.uid())}>",
            "      <auth-provider>pgpass</auth-provider>",
            f"      <user-name>{escape(item.user)}</user-name>",
            "      <schema-mapping>",
            "        <introspection-scope>",
            '          <node kind="database" qname="@">',
            f'            <node kind="schema" qname={quoteattr(item.schema)} />',
            "          </node>",
            "        </introspection-scope>",
            "      </schema-mapping>",
            "    </data-source>",
        ]
    closing = ["  </component>", "</project>", ""]
    return "\n".join(shared + closing), "\n".join(local + closing)


def merge_xml(path: Path, ours: str) -> str:
    """A JetBrains data sources file with ours put in by uuid; the project's other data sources stay."""
    if not path.exists():
        return ours
    try:
        # The user's own project file. Expat in the standard library expands no external entities
        # and, since 2.4.1, refuses entity bombs; defusedxml would be a dependency.
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))  # noqa: S314
        root = ET.fromstring(path.read_text(encoding="utf-8"), parser=parser)  # noqa: S314
    except (ET.ParseError, OSError, UnicodeDecodeError) as exc:
        raise ExportRefusedError(t("export.unreadable", path=path, error=exc)) from exc
    fresh = ET.fromstring(ours).find("component")  # noqa: S314  (our own text)
    if fresh is None:  # pragma: no cover - datagrip() always writes one
        return ours
    component = root.find(f"component[@name='{fresh.get('name')}']")
    if component is None:
        root.append(fresh)
    else:
        replaced = {source.get("uuid") for source in fresh.findall("data-source")}
        for source in component.findall("data-source"):
            if source.get("uuid") in replaced:
                component.remove(source)
        component.extend(fresh.findall("data-source"))
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# --- pgAdmin 4 ----------------------------------------------------------------------------------------


def pgadmin(items: Sequence[Connection], passfile: str) -> str:
    """servers.json for Tools | Import/Export Servers: a group per stand, the database only, the password file.

    pgAdmin has no default schema; PostConnectionSQL puts the schema first on the search path instead.
    """
    servers = {}
    for number, item in enumerate(items, start=1):
        parameters: dict[str, Any] = {"sslmode": item.sslmode or "prefer", "connect_timeout": 10, "passfile": passfile}
        if item.sslrootcert:
            parameters["sslrootcert"] = item.sslrootcert
        servers[str(number)] = {
            "Name": item.label,
            "Group": item.stand,
            "Host": item.host,
            "Port": item.port,
            "MaintenanceDB": item.dbname,
            "Username": item.user,
            "DBRestriction": item.dbname,
            "DBRestrictionType": "databases",
            "PostConnectionSQL": f"SET search_path TO {quote_ident(item.schema)}, public",
            "ConnectionParameters": parameters,
        }
    return json.dumps({"Servers": servers}, indent=2, ensure_ascii=False) + "\n"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --- VS Code extensions: PostgreSQL by Microsoft and SQLTools ------------------------------------------

OURS = "pg-mcp-"  # the prefix of the ids an export gives to the PostgreSQL extension's groups and connections


def vscode_pgsql(items: Sequence[Connection], settings: dict[str, Any]) -> dict[str, Any]:
    """The settings with the PostgreSQL extension's groups and connections of these stands, by id.

    The extension cannot read the password file yet: it asks for a password once and keeps it in the keychain.
    """
    stands = list(dict.fromkeys(item.stand for item in items))
    group_ids = {f"{OURS}{stand}" for stand in stands}
    ids = {f"{OURS}{item.uid()}" for item in items}
    groups = [group for group in _items(settings, "pgsql.serverGroups") if group.get("id") not in group_ids]
    saved = [entry for entry in _items(settings, "pgsql.connections") if entry.get("id") not in ids]
    groups.extend({"id": f"{OURS}{stand}", "name": stand} for stand in stands)
    for item in items:
        entry: dict[str, Any] = {
            "id": f"{OURS}{item.uid()}",
            "profileName": item.label,
            "groupId": f"{OURS}{item.stand}",
            "server": item.host,
            "port": str(item.port),
            "database": item.dbname,
            "user": item.user,
            "authenticationType": "SqlLogin",
            "options": f"-c search_path={item.schema},public",
            **tls_settings(item),
        }
        saved.append(entry)
    return {**settings, "pgsql.serverGroups": groups, "pgsql.connections": saved}


def sqltools(items: Sequence[Connection], settings: dict[str, Any]) -> dict[str, Any]:
    """The settings with SQLTools connections of these stands; same-named connections are replaced.

    The empty password leaves node-postgres to find the password in the password file.
    """
    labels = {item.label for item in items}
    saved = [entry for entry in _items(settings, "sqltools.connections") if entry.get("name") not in labels]
    for item in items:
        entry: dict[str, Any] = {
            "name": item.label,
            "group": item.stand,
            "driver": "PostgreSQL",
            "server": item.host,
            "port": item.port,
            "database": item.dbname,
            "username": item.user,
            "password": "",
            "connectionTimeout": 15,
            "previewLimit": 50,
        }
        if item.sslmode in ("require", "prefer", "allow"):
            entry["pgOptions"] = {"ssl": {"rejectUnauthorized": False}}  # TLS without checking the certificate
        elif item.sslmode in ("verify-ca", "verify-full"):
            # SQLTools reads the CA file itself and gives node-postgres its text
            ssl = {"rejectUnauthorized": True, "ca": item.sslrootcert} if item.sslrootcert else True
            entry["pgOptions"] = {"ssl": ssl}
        saved.append(entry)
    return {**settings, "sqltools.connections": saved}


def _items(settings: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = settings.get(key, [])
    if not isinstance(value, list):
        raise ExportRefusedError(t("export.not_a_list", setting=key))
    return [entry for entry in value if isinstance(entry, dict)]


def read_settings(path: Path) -> dict[str, Any] | None:
    """A JSON settings file as an object; {} when there is none, None when it is not plain JSON."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportRefusedError(t("export.unreadable", path=path, error=exc)) from exc
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except ValueError:
        return None
    return settings if isinstance(settings, dict) else None


# --- the tools, where each one's files go, and what to do next ----------------------------------------

TOOLS = {
    "dbeaver": "DBeaver",
    "datagrip": "DataGrip / JetBrains",
    "pgadmin": "pgAdmin 4",
    "pg-service": "psql, pgcli (pg_service.conf)",
    "vscode-pgsql": "VS Code: PostgreSQL",
    "sqltools": "VS Code: SQLTools",
}
NEXT = {
    "dbeaver": "export.next_dbeaver",
    "datagrip": "export.next_datagrip",
    "pgadmin": "export.next_pgadmin",
    "pg-service": "export.next_pg_service",
    "vscode-pgsql": "export.next_vscode_pgsql",
    "sqltools": "export.next_sqltools",
}
EDITORS = {"vscode": ("Code",), "cursor": ("Cursor",), "windsurf": ("Windsurf", "Devin")}  # their settings folders
EXTENSIONS = {"vscode-pgsql": "ms-ossdata.vscode-pgsql-", "sqltools": "mtxr.sqltools-"}
EXTENSION_FOLDERS = {"vscode": ".vscode", "cursor": ".cursor", "windsurf": ".windsurf"}
# The IDEs of JetBrains with the database tools; their settings folders start with these names.
JETBRAINS_DATABASE_IDES = ("DataGrip", "IntelliJIdea", "PyCharm", "GoLand", "PhpStorm", "RubyMine", "Rider", "CLion")


@dataclass(frozen=True)
class Export:
    """What an export would write: the connections, the files with their new text, and what to do next."""

    tool: str
    items: list[Connection]  # what goes in: the connections the tool does not have of its own
    files: list[tuple[Path, str]]  # empty when the tool has every connection already
    next_step: str
    notes: list[str] = field(default_factory=list)  # what was left out, and why


def default_project(env: Mapping[str, str]) -> Path:
    """The folder to open as a DataGrip project when no other is named: next to the stands registry."""
    return registry_files(env)[0].parent / "datagrip"


def prepare(
    tool: str,
    items: Sequence[Connection],
    env: Mapping[str, str],
    home: Path,
    *,
    workspace: Path | None = None,
    project: Path | None = None,
    editor: str = "vscode",
) -> Export:
    """The files a tool gets for these connections; nothing is written yet."""
    if tool == "dbeaver":
        return _dbeaver_export(items, env, home, workspace)
    if tool == "datagrip":
        target = project or default_project(env)
        idea = target / ".idea"
        shared, local = datagrip(items)
        files = [
            (idea / name, merge_xml(idea / name, text))
            for name, text in (("dataSources.xml", shared), ("dataSources.local.xml", local))
        ]
        return Export(tool, list(items), files, t(NEXT[tool], path=target))
    if tool == "pgadmin":
        passfile = str(Path(pgpass_path(env)).expanduser().resolve())
        servers = registry_files(env)[0].parent / "pgadmin-servers.json"
        next_step = t(NEXT[tool], path=servers, passfile=passfile)
        return Export(tool, list(items), [(servers, pgadmin(items, passfile))], next_step)
    if tool == "pg-service":
        return _service_export(items, env)
    return _editor_export(tool, items, env, home, editor)


def _dbeaver_export(items: Sequence[Connection], env: Mapping[str, str], home: Path, workspace: Path | None) -> Export:
    """Into pg-explorer-mcp's own file of the DBeaver workspace, leaving out what DBeaver has of its own.

    The stands most often come from DBeaver in the first place (install.py import dbeaver).
    """
    if workspace is not None and not workspace.is_dir():
        raise ExportRefusedError(t("export.not_a_folder", path=workspace))
    folder = workspace or dbeaver_workspace(env, home)
    if folder is None:
        raise ExportRefusedError(t("export.no_dbeaver"))
    fresh = not_in_dbeaver(items, in_dbeaver(folder))
    notes = [t("export.present_dbeaver", count=len(items) - len(fresh))] if len(fresh) < len(items) else []
    path = folder / DBEAVER_FILE
    files = [(path, dbeaver(fresh, read_settings(path)))] if fresh else []
    return Export("dbeaver", fresh, files, t(NEXT["dbeaver"]), notes)


def _service_export(items: Sequence[Connection], env: Mapping[str, str]) -> Export:
    """Into the user's pg_service.conf, leaving out the stands that have a section the user wrote there.

    That includes the stands the registry reads from this very file.
    """
    service = registry_files(env)[1]
    try:
        existing = service.read_text(encoding="utf-8") if service.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportRefusedError(t("export.unreadable", path=service, error=exc)) from exc
    sections = service_sections(existing)
    fresh = [item for item in items if sections.get(item.key, True)]  # no section yet, or one an export wrote
    notes = (
        [t("export.present_service", path=service, count=len(items) - len(fresh))] if len(fresh) < len(items) else []
    )
    if not fresh:
        return Export("pg-service", [], [], "", notes)
    next_step = t(NEXT["pg-service"], service=fresh[0].key)
    return Export("pg-service", fresh, [(service, pg_service(fresh, existing))], next_step, notes)


def _editor_export(tool: str, items: Sequence[Connection], env: Mapping[str, str], home: Path, editor: str) -> Export:
    """Into the user settings of VS Code or a fork of it; a file with comments gets the text to add instead."""
    path = editor_settings(env, home, editor)
    if not path.parent.is_dir():
        raise ExportRefusedError(t("export.no_editor", editor=editor, path=path.parent))
    change = vscode_pgsql if tool == "vscode-pgsql" else sqltools
    settings = read_settings(path)
    if settings is None:
        snippet = json.dumps(change(items, {}), indent=4, ensure_ascii=False)
        raise ExportRefusedError(t("export.not_json", path=path, snippet=snippet))
    text = json.dumps(change(items, settings), indent=4, ensure_ascii=False) + "\n"
    return Export(tool, list(items), [(path, text)], t(NEXT[tool]))


def editor_settings(env: Mapping[str, str], home: Path, editor: str) -> Path:
    """The user settings.json of VS Code or a fork of it; the first of its folder names that exists."""
    folders = [app_data(env, home) / name / "User" for name in EDITORS[editor]]
    return next((folder for folder in folders if folder.is_dir()), folders[0]) / "settings.json"


def preview(export: Export) -> list[str]:
    """What the export would do, for the screen: the connections, the files, what is left out."""
    lines = []
    if export.items:
        stands = summarize(list(dict.fromkeys(item.stand for item in export.items)))
        lines.append(plural("export.preview", len(export.items), tool=TOOLS[export.tool], stands=stands))
        lines.extend(f"  {path}" for path, _ in export.files)
    lines.extend(export.notes)
    if not export.files:
        lines.append(t("export.nothing_new", tool=TOOLS[export.tool]))
    return lines


def write(export: Export) -> list[str]:
    """Write the files, each with a dated copy of the old one; the lines that say what was done.

    Every new text goes into a file next to its place first, so a folder that cannot be written
    changes nothing. The files are the user's own (a pg_service.conf may hold passwords of other
    services): a new text is readable by its owner only until it takes the old file's permissions.
    """
    staged: list[tuple[Path, Path, Path]] = []  # the path as named, the file it is, the new text next to that file
    try:
        for path, text in export.files:
            staged.append(_stage(path, text))
    except OSError as exc:
        _discard(staged)
        raise ExportRefusedError(t("import.cannot_write", path=path, error=exc)) from exc
    lines: list[str] = []
    for number, (path, real, temporary) in enumerate(staged):
        try:
            backup = _place(real, temporary)
        except OSError as exc:
            _discard(staged[number:])
            raise ExportRefusedError("\n".join([*lines, t("export.cannot_place", path=path, error=exc)])) from exc
        lines.append(
            t("import.written", path=path) if backup is None else t("import.written_backup", path=path, backup=backup)
        )
    return [*lines, export.next_step]


def _stage(path: Path, text: str) -> tuple[Path, Path, Path]:
    """Write the new text next to the file the path is, readable by its owner only."""
    real = path.resolve()  # a link (into a dotfiles repository, say) stays a link to the changed file
    temporary = real.with_name(real.name + ".pg-explorer-mcp-new")
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
    except OSError:
        _discard([(path, real, temporary)])
        raise
    return path, real, temporary


def _place(path: Path, temporary: Path) -> Path | None:
    """Put the new file in place: the old one becomes the dated copy and gives the new one its permissions."""
    if not path.exists():
        temporary.replace(path)
        return None
    backup = backup_name(path)
    shutil.copymode(path, temporary)
    path.replace(backup)
    try:
        temporary.replace(path)
    except OSError:
        backup.replace(path)  # the old file goes back where it was
        raise
    return backup


def backup_name(path: Path) -> Path:
    """A dated name for the old file that no earlier copy has: two writes within a second keep both copies."""
    stamp = f"{path.name}.bak-{_now()}"
    backup, number = path.with_name(stamp), 1
    while backup.exists():
        number += 1
        backup = path.with_name(f"{stamp}-{number}")
    return backup


def _now() -> str:
    return f"{datetime.datetime.now():%Y%m%d-%H%M%S}"


def _discard(staged: Sequence[tuple[Path, Path, Path]]) -> None:
    for _, _, temporary in staged:
        with contextlib.suppress(OSError):  # not there, or not even its folder
            temporary.unlink()


def installed(env: Mapping[str, str], home: Path) -> list[tuple[str, str]]:
    """The tools that seem to be here, as (tool, editor); the editor is set for the VS Code extensions only."""
    found: list[tuple[str, str]] = []
    if dbeaver_workspace(env, home) is not None:
        found.append(("dbeaver", ""))
    jetbrains = app_data(env, home) / "JetBrains"
    if jetbrains.is_dir() and any(child.name.startswith(JETBRAINS_DATABASE_IDES) for child in jetbrains.iterdir()):
        found.append(("datagrip", ""))
    if (home / ".pgadmin").is_dir() or (app_data(env, home) / "pgAdmin").is_dir():
        found.append(("pgadmin", ""))
    if shutil.which("psql") or shutil.which("pgcli"):
        found.append(("pg-service", ""))
    for tool, prefix in EXTENSIONS.items():
        for editor, folder in EXTENSION_FOLDERS.items():
            extensions = home / folder / "extensions"
            if extensions.is_dir() and any(child.name.startswith(prefix) for child in extensions.iterdir()):
                found.append((tool, editor))
                break
    return found
