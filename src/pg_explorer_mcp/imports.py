"""Import of stand connections from DBeaver or pg_service.conf into the stands registry (ADR 0006).

The import never runs inside the server: install.py calls it, previews by default and writes
only on request. Writing is all or nothing as far as two files allow: both are prepared in
full before either is put in place, each gets a backup, and the server keeps working with
whatever it had if anything fails. Passwords never reach the screen.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .aes import decrypt_cbc
from .db import pgpass_path, split_hosts
from .i18n import t
from .stands import Target, exported_sections, parse_service_file, registry_files

# DBeaver encrypts credentials-config.json with this key unless the user turned on its secure storage.
DBEAVER_KEY = bytes.fromhex("babb4a9f774ab853c96c2d653dfe544a")
DBEAVER_PROVIDER = "postgresql"
DEFAULT_STAND_PATTERN = r"(dev|ift)\d+"
_NAME = re.compile(r"[A-Za-z0-9_$-]+")  # what a part of a stand.database.schema section may hold
_FILE_MODE = 0o600


class ImportRefusedError(Exception):
    """The import could not be done; the message says why. Nothing was written."""


@dataclass(frozen=True)
class Connection:
    """One connection as its source describes it, before a stand is recognised."""

    source: str  # where it came from, for the preview
    name: str
    folder: str
    host: str  # one host, or several separated by commas, as libpq takes them
    port: str  # one port for every host, or one per host separated by commas
    database: str
    user: str
    password: str | None
    options: Mapping[str, str] = field(default_factory=dict)  # sslmode and the like

    @property
    def endpoints(self) -> list[tuple[str, str]]:
        """Each host with its port, in order; the port of every host when there is one port."""
        hosts = [host.strip() for host in self.host.split(",")]
        ports = [port.strip() for port in self.port.split(",")]
        return list(zip(hosts, ports * len(hosts) if len(ports) == 1 else ports))

    @property
    def address(self) -> str:
        return ",".join(f"{host}:{port}" for host, port in self.endpoints)


@dataclass
class Plan:
    """What an import would do: the targets to register, their passwords and what is left out."""

    targets: list[Target] = field(default_factory=list)
    passwords: dict[tuple[str, str, str, str], str] = field(default_factory=dict)  # host, port, db, user
    without_password: list[Target] = field(default_factory=list)
    skipped: list[tuple[Connection, str]] = field(default_factory=list)


# --- reading -------------------------------------------------------------------------------------


def dbeaver_workspace(env: Mapping[str, str], home: Path | None = None) -> Path | None:
    """DBeaver's connection folder on this machine, if there is one."""
    home = home or Path.home()
    candidates = [home / ".local" / "share" / "DBeaverData", home / "Library" / "DBeaverData"]
    if env.get("APPDATA"):
        candidates.insert(0, Path(env["APPDATA"]) / "DBeaverData")
    for root in candidates:
        folder = root / "workspace6" / "General" / ".dbeaver"
        if (folder / "data-sources.json").is_file():
            return folder
    return None


def read_dbeaver(folder: Path) -> list[Connection]:
    """PostgreSQL connections of a DBeaver workspace with their saved logins."""
    try:
        sources = json.loads((folder / "data-sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportRefusedError(t("import.unreadable", path=folder / "data-sources.json", error=exc)) from exc
    credentials = _dbeaver_credentials(folder / "credentials-config.json")
    connections = []
    for identifier, entry in sources.get("connections", {}).items():
        if entry.get("provider") != DBEAVER_PROVIDER:
            continue
        configuration = entry.get("configuration", {})
        saved = credentials.get(identifier, {}).get("#connection", {})
        host, port, database = dbeaver_address(configuration)
        connections.append(
            Connection(
                source="DBeaver",
                name=str(entry.get("name", identifier)),
                folder=str(entry.get("folder", "")),
                host=host,
                port=port,
                database=database,
                user=str(saved.get("user") or configuration.get("user") or ""),
                password=saved.get("password") or None,
            )
        )
    return connections


def _dbeaver_credentials(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    """The decrypted credentials, or nothing when the file is absent or kept in secure storage."""
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    try:
        text = decrypt_cbc(DBEAVER_KEY, raw[:16], raw[16:]).decode("utf-8")
        decoded = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return {}  # secure storage, or another DBeaver: the logins are then added by hand
    return decoded if isinstance(decoded, dict) else {}


def dbeaver_address(configuration: Mapping[str, object]) -> tuple[str, str, str]:
    """Host, port and database of a DBeaver connection, the hosts as a libpq list when there are several.

    DBeaver keeps a connection with several hosts as the host field "h1:6544,h2" with the port field
    6544, and builds the URL jdbc:postgresql://h1:6544,h2:6544/db from them: a host without a port of
    its own takes the port field. A connection made by URL has the hosts in the URL instead, and so
    does one whose host field is empty.
    """
    host = str(configuration.get("host") or "")
    port = str(configuration.get("port") or "")
    database = str(configuration.get("database") or "")
    match = re.match(r"jdbc:postgresql://([^/?]+)/([^?]*)", str(configuration.get("url") or ""))
    by_url = configuration.get("configurationType") == "URL" or not host
    pairs = _endpoints(match.group(1) if by_url and match else host, port or "5432")
    database = database or (match.group(2) if match else "")
    if not pairs:
        return "", port or "5432", database
    numbers = [number for _, number in pairs]
    return ",".join(name for name, _ in pairs), numbers[0] if len(set(numbers)) == 1 else ",".join(numbers), database


def _endpoints(text: str, default_port: str) -> list[tuple[str, str]]:
    """The host:port pairs of a list such as "h1:6544,h2": a host without a port of its own takes the default."""
    pairs = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        name, colon, number = item.rpartition(":")
        if colon and name and number.isdigit():
            pairs.append((name, number))
        else:
            pairs.append((item, default_port))
    return pairs


def read_pg_service(path: Path) -> list[Connection]:
    """Every section of a pg_service.conf file as a connection, but for the copies install.py export wrote."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImportRefusedError(t("import.unreadable", path=path, error=exc)) from exc
    sections, _ = parse_service_file(text)
    copies = exported_sections(text)
    extras = ("sslmode", "sslrootcert", "connect_timeout")
    return [
        Connection(
            source="pg_service.conf",
            name=name,
            folder="",
            host=options.get("host", ""),
            port=options.get("port", "5432"),
            database=options.get("dbname", ""),
            user=options.get("user", ""),
            password=options.get("password") or None,
            options={key: options[key] for key in extras if options.get(key)},
        )
        for name, options in sections.items()
        if name not in copies
    ]


# --- planning ------------------------------------------------------------------------------------


def stand_of(connection: Connection, pattern: re.Pattern[str]) -> str | None:
    """The stand named in the folder, the connection name or the host, in that order; lower case."""
    for text in (connection.folder, connection.name, connection.host):
        match = pattern.search(text)
        if match:
            found = match.group("stand") if "stand" in pattern.groupindex else match.group(0)
            return found.lower()
    return None


def compile_pattern(raw: str) -> re.Pattern[str]:
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error as exc:
        raise ImportRefusedError(t("import.bad_pattern", pattern=raw, error=exc)) from exc


def plan(connections: Iterable[Connection], pattern: re.Pattern[str]) -> Plan:
    """Targets for the registry: a stand from the names, the database as named, the user as the schema."""
    result = Plan()
    seen: dict[str, Connection] = {}
    for connection in connections:
        stand = stand_of(connection, pattern)
        problem = _problem(connection, stand)
        if problem is not None:
            result.skipped.append((connection, problem))
            continue
        target = Target(str(stand), connection.database, connection.user, _options(connection), connection.source)
        if target.name in seen:
            result.skipped.append((connection, t("import.duplicate", name=target.name, other=seen[target.name].name)))
            continue
        seen[target.name] = connection
        result.targets.append(target)
        if connection.password is None:
            result.without_password.append(target)
        else:
            for host, port in connection.endpoints:  # libpq matches a password line against one host
                result.passwords[host, port, connection.database, connection.user] = connection.password
    return result


def _problem(connection: Connection, stand: str | None) -> str | None:
    if not connection.host:
        return t("import.no_host")
    try:
        split_hosts(connection.host, connection.port)
    except ValueError as exc:  # a port that is not a number, or a count of ports that matches no host
        return str(exc)
    if not connection.database:
        return t("import.no_database")
    if not connection.user:
        return t("import.no_user")
    if stand is None:
        return t("import.no_stand")
    for part in (stand, connection.database, connection.user):
        if not _NAME.fullmatch(part):
            return t("import.bad_name", part=part)
    return None


def _options(connection: Connection) -> dict[str, str]:
    options = {"host": connection.host}
    if connection.port != "5432":
        options["port"] = connection.port
    options.update(connection.options)
    return options


# --- the preview ---------------------------------------------------------------------------------


def preview(result: Plan, registry_file: Path, passfile: Path) -> list[str]:
    """Lines for the screen: what goes where, without a single password."""
    lines = [t("import.preview_header", count=len(result.targets), registry=registry_file)]
    for target in result.targets:
        note = "" if target not in result.without_password else "  " + t("import.preview_no_password")
        lines.append(f"  [{target.name}]  {target.address}{note}")
    lines.append(t("import.preview_passwords", count=len(result.passwords), passfile=passfile))
    if result.skipped:
        lines.append(t("import.preview_skipped", count=len(result.skipped)))
        lines.extend(f"  {connection.source}: {connection.name}  {reason}" for connection, reason in result.skipped)
    return lines


# --- writing -------------------------------------------------------------------------------------


def write(result: Plan, registry_file: Path, passfile: Path, *, prune: bool = False) -> list[str]:
    """Put the registry and the password file in place; both are prepared in full before either is."""
    if not result.targets:
        raise ImportRefusedError(t("import.nothing"))
    registry_text = _registry_text(registry_file, result.targets, prune=prune)
    passfile_text = _passfile_text(passfile, result.passwords)
    prepared = ((registry_file, registry_text), (passfile, passfile_text))
    try:
        for path, text in prepared:
            path.parent.mkdir(parents=True, exist_ok=True)
            _prepare(path, text)
    except OSError as exc:
        _discard(registry_file, passfile)
        raise ImportRefusedError(t("import.cannot_write", path=exc.filename or registry_file, error=exc)) from exc
    lines = []
    for path, _ in prepared:
        backup = _place(path)
        lines.append(
            t("import.written", path=path) if backup is None else t("import.written_backup", path=path, backup=backup)
        )
    return lines


def _registry_text(registry_file: Path, targets: Sequence[Target], *, prune: bool) -> str:
    sections: dict[str, dict[str, str]] = {}
    if not prune:
        try:
            sections, _ = parse_service_file(registry_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            sections = {}
    for target in targets:
        sections[target.name] = dict(target.options)
    when = datetime.date.today().isoformat()
    out = [
        f"# Stands of pg-explorer-mcp, written by install.py import on {when}. Sections are stand.database.schema;",
        "# the server reads this file when it starts. Passwords belong in the password file.",
        "",
    ]
    for name in sorted(sections, key=_natural_key):
        out.append(f"[{name}]")
        out.extend(f"{key}={value}" for key, value in sections[name].items())
        out.append("")
    return "\n".join(out)


def _natural_key(name: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _passfile_text(passfile: Path, passwords: Mapping[tuple[str, str, str, str], str]) -> str:
    """The password file with these logins added or updated and everything else kept as it was."""
    try:
        existing = passfile.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        existing = []
    pending = dict(passwords)
    out = []
    for line in existing:
        key = _passfile_key(line)
        if key in pending:
            out.append(_passfile_line(key, pending.pop(key)))
        else:
            out.append(line)
    out.extend(_passfile_line(key, password) for key, password in pending.items())
    return "\n".join(out) + ("\n" if out else "")


def _passfile_key(line: str) -> tuple[str, str, str, str] | None:
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    fields = _split(line)
    return (fields[0], fields[1], fields[2], fields[3]) if len(fields) == 5 else None


def _split(line: str) -> list[str]:
    fields, current, escaped = [], "", False
    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(current)
            current = ""
        else:
            current += char
    fields.append(current)
    return fields


def _passfile_line(key: tuple[str, str, str, str], password: str) -> str:
    return ":".join(_escape(part) for part in (*key, password))


def _escape(part: str) -> str:
    return part.replace("\\", "\\\\").replace(":", "\\:")


def _temporary(path: Path) -> Path:
    return path.with_name(path.name + ".pg-explorer-mcp-new")


def _prepare(path: Path, text: str) -> None:
    """Write the new content next to the file, readable by its owner only, flushed to disk."""
    temporary = _temporary(path)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if sys.platform != "win32":
        temporary.chmod(_FILE_MODE)


def _place(path: Path) -> Path | None:
    """Move the prepared content over the file, keeping the old file as a dated backup."""
    backup = None
    if path.exists():
        when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{when}")
        path.replace(backup)
        if sys.platform != "win32" and stat.S_IMODE(backup.stat().st_mode) & 0o077:
            backup.chmod(_FILE_MODE)  # a backup of passwords should be no more readable than the file
    _temporary(path).replace(path)
    return backup


def _discard(*paths: Path) -> None:
    for path in paths:
        temporary = _temporary(path)
        if temporary.exists():
            temporary.unlink()


def default_files(env: Mapping[str, str]) -> tuple[Path, Path]:
    """Where the import writes: the server's own stands file and the libpq password file."""
    return registry_files(env)[0], Path(pgpass_path(env))
