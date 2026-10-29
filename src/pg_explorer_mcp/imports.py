"""Import of stand connections from DBeaver, DataGrip or pg_service.conf into the stands registry (ADR 0006).

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
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus

from .aes import decrypt_cbc
from .clients import app_data
from .db import pgpass_path, split_hosts
from .i18n import t
from .stands import Target, export_uuid, exported_sections, parse_service_file, registry_files

# DBeaver encrypts credentials-config.json with this key unless the user turned on its secure storage.
DBEAVER_KEY = bytes.fromhex("babb4a9f774ab853c96c2d653dfe544a")
DBEAVER_PROVIDER = "postgresql"
DATAGRIP_DRIVER = "postgresql"  # the driver-ref of a PostgreSQL data source in the IDEs of JetBrains
JDBC_URL = re.compile(r"jdbc:postgresql://([^/?]*)/?([^?]*)\??(.*)", re.DOTALL)  # hosts, database, parameters
_IDE_FOLDER = re.compile(r"([A-Za-z]+)(\d{4})\.(\d+)")  # the settings of one version of an IDE: DataGrip2026.2
DEFAULT_STAND_PATTERN = r"(dev|ift)[-_ ]?\d+"  # letters, then a number, with or without a separator between
_NAME = re.compile(r"[A-Za-z0-9_$-]+")  # what a part of a stand.database.schema section may hold
_SEPARATORS = re.compile(r"[-_ ]+")  # what a stand the pattern found loses: DEV-27 is the stand dev27
_NAME_WORD = re.compile(r"[a-z0-9_$]+")  # the words of a connection name that may make a stand name
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
    tunnel: str | None = None  # through an SSH tunnel: the SSH server, or "" when the source does not name it

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
    named: list[Target] = field(default_factory=list)  # whose stand is the connection name: the pattern found none


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
                tunnel=dbeaver_tunnel(configuration),
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

    The URL comes first and the fields fill in what it lacks: a connection made by URL keeps DBeaver's
    defaults in its fields, "postgres" as the database among them, and a connection made by fields has
    its URL built from them anyway. DBeaver keeps several hosts as the host field "h1:6544,h2" with the
    port field 6544 and the URL jdbc:postgresql://h1:6544,h2:6544/db: a host without a port of its own
    takes the port field.
    """
    host = str(configuration.get("host") or "")
    port = str(configuration.get("port") or "")
    database = str(configuration.get("database") or "")
    match = re.match(r"jdbc:postgresql://([^/?]+)/?([^?]*)", str(configuration.get("url") or ""))
    pairs = _endpoints(match.group(1) if match else host, port or "5432")
    database = (match.group(2) if match else "") or database
    if not pairs:
        return "", port or "5432", database
    return (*_hosts_and_ports(pairs), database)


def _endpoints(text: str, default_port: str) -> list[tuple[str, str]]:
    """The host:port pairs of a list such as "h1:6544,h2": a host without a port of its own takes the default.

    An IPv6 address loses its brackets, [::1]:5432 being ::1 with the port 5432: libpq takes it bare.
    """
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
    return [(name[1:-1] if name.startswith("[") and name.endswith("]") else name, number) for name, number in pairs]


def _hosts_and_ports(pairs: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """The hosts as a libpq list, with one port for all of them or, when they differ, a port for each."""
    numbers = [number for _, number in pairs]
    return ",".join(name for name, _ in pairs), numbers[0] if len(set(numbers)) == 1 else ",".join(numbers)


def dbeaver_tunnel(configuration: Mapping[str, object]) -> str | None:
    """The SSH server of a connection DBeaver makes through an SSH tunnel, "" when unnamed; None for a direct one.

    The tunnel is one of the connection's network handlers, next to its TLS settings (postgre_ssl) and its
    SOCKS proxy (socks_proxy). DBeaver writes only the handlers that are switched on, and a network profile
    copies its own into each connection that takes it.
    """
    handlers = configuration.get("handlers")
    tunnel = handlers.get("ssh_tunnel") if isinstance(handlers, dict) else None
    if not isinstance(tunnel, dict) or not _switched_on(tunnel.get("enabled")):
        return None
    properties = tunnel.get("properties")
    return _ssh_address(properties.get("host"), properties.get("port")) if isinstance(properties, dict) else ""


def _switched_on(value: object) -> bool:
    return str(value).strip().lower() == "true"  # true in JSON, or the text of an XML element


def _ssh_address(host: object, port: object) -> str:
    """host:port of an SSH server, the host alone when no port is written; DBeaver may write the port 22 as 22.0."""
    name, number = str(host or "").strip(), str(port or "").strip().removesuffix(".0")
    return f"{name}:{number}" if name and number else name


def datagrip_folders(env: Mapping[str, str], home: Path, project: Path | None = None) -> list[Path]:
    """The folders with JetBrains data sources to read: the project named, DataGrip's projects, the global ones.

    DataGrip keeps its data sources in its projects, in ~/DataGripProjects since 2021.2; the other IDEs
    keep theirs in the projects of code, which only the project named brings in. The global data sources
    are those of the newest version of each IDE that has any: a new version copies the settings of the
    one before, so an older folder holds only what was there then.
    """
    folders = [] if project is None else [project if (project / "dataSources.xml").is_file() else project / ".idea"]
    folders.extend(path.parent for path in sorted((home / "DataGripProjects").glob("*/.idea/dataSources.xml")))
    versions = [version for version in _ide_versions(env, home) if (version[3] / "dataSources.xml").is_file()]
    newest = {ide: options for ide, _, _, options in versions}  # each IDE's newest version comes last
    folders.extend(newest.values())
    unique: dict[Path, Path] = {}
    for folder in folders:  # a project named that is one of DataGrip's is read once
        unique.setdefault(folder.resolve(), folder)
    return list(unique.values())


def _ide_versions(env: Mapping[str, str], home: Path) -> list[tuple[str, int, int, Path]]:
    """The options folder of each version of each JetBrains IDE on this machine, by IDE, the oldest version first."""
    jetbrains = app_data(env, home) / "JetBrains"
    versions = []
    for child in jetbrains.iterdir() if jetbrains.is_dir() else []:
        match = _IDE_FOLDER.fullmatch(child.name)
        if match:
            versions.append((match.group(1), int(match.group(2)), int(match.group(3)), child / "options"))
    return sorted(versions)


def jetbrains_ssh_configs(env: Mapping[str, str], home: Path) -> dict[str, str]:
    """The SSH configurations the JetBrains IDEs on this machine share between projects: the server of each, by id.

    Each version of an IDE keeps them in sshConfigs.xml of its options folder and copies those of the
    version before; the newest version's are read last and win.
    """
    configs: dict[str, str] = {}
    for *_, options in _ide_versions(env, home):
        configs.update(_ssh_configs(options / "sshConfigs.xml"))
    return configs


def read_datagrip(folder: Path, ssh_configs: Mapping[str, str] | None = None) -> list[Connection]:
    """PostgreSQL data sources of a JetBrains project, or the global ones of an IDE, with their users.

    folder is a project or the folder that holds dataSources.xml: the .idea of a project, or the options
    folder of an IDE for its global data sources. That file has the names, groups and URLs; the user of
    each data source is in dataSources.local.xml next to it, and so is its SSH tunnel. The tunnel names
    an SSH configuration, kept in sshConfigs.xml next to them or among ssh_configs, those an IDE shares
    between its projects (jetbrains_ssh_configs). The passwords stay where the IDE keeps them, in the
    keychain of the system or in KeePass, and go into the password file by hand. The data sources
    install.py export wrote are not brought back: their uuid is the one export gives the section their
    name spells, stand / database / schema.
    """
    if not (folder / "dataSources.xml").is_file():
        folder = folder / ".idea"
    local = folder / "dataSources.local.xml"
    local_sources = list(_xml(local).iter("data-source")) if local.is_file() else []
    users = {source.get("uuid"): (source.findtext("user-name") or "").strip() for source in local_sources}
    servers = {**(ssh_configs or {}), **_ssh_configs(folder / "sshConfigs.xml")}
    tunnels = {source.get("uuid"): datagrip_tunnel(source.find("ssh-properties"), servers) for source in local_sources}
    connections = []
    for source in _xml(folder / "dataSources.xml").iter("data-source"):
        name, identifier = source.get("name", ""), source.get("uuid", "")
        exported = identifier == export_uuid(name.replace(" / ", "."))
        if exported or (source.findtext("driver-ref") or "").strip() != DATAGRIP_DRIVER:
            continue
        host, port, database, parameters = jdbc_address(source.findtext("jdbc-url") or "")
        connections.append(
            Connection(
                source="DataGrip",
                name=name or identifier,
                folder=source.get("group", ""),
                host=host,
                port=port,
                database=database,
                user=parameters.get("user") or users.get(identifier, ""),  # pgjdbc lets the URL override the user
                password=parameters.get("password") or None,  # a URL keeps it in plain text; the IDE never does
                options={key: parameters[key] for key in ("sslmode", "sslrootcert") if parameters.get(key)},
                tunnel=tunnels.get(identifier),
            )
        )
    return connections


def datagrip_tunnel(ssh: ET.Element | None, ssh_configs: Mapping[str, str]) -> str | None:
    """The SSH server of a data source an IDE reaches through an SSH tunnel, "" when unnamed; None for a direct one.

    ssh is the data source's ssh-properties in dataSources.local.xml. A newer IDE names the SSH configuration
    there by its id (ssh-config-id), which ssh_configs maps to its server; an older one wrote the server in
    place (proxy-host, proxy-port). A tunnel switched off leaves the data source direct.
    """
    if ssh is None or not _switched_on(ssh.findtext("enabled")):
        return None
    config = (ssh.findtext("ssh-config-id") or "").strip()
    if config:
        return ssh_configs.get(config, "")
    return _ssh_address(ssh.findtext("proxy-host"), ssh.findtext("proxy-port"))


def _ssh_configs(path: Path) -> dict[str, str]:
    """The SSH servers of a JetBrains sshConfigs.xml by the id of their configuration; nothing when it is unreadable.

    They only name the server in the preview, so a missing or broken file costs that name, not the import.
    """
    try:
        root = _xml(path)
    except ImportRefusedError:
        return {}
    return {
        config.get("id", ""): _ssh_address(config.get("host"), config.get("port")) for config in root.iter("sshConfig")
    }


def _xml(path: Path) -> ET.Element:
    try:
        # The user's own IDE file. Expat in the standard library expands no external entities and,
        # since 2.4.1, refuses entity bombs; defusedxml would be a dependency.
        return ET.parse(path).getroot()  # noqa: S314
    except (OSError, ET.ParseError) as exc:
        raise ImportRefusedError(t("import.unreadable", path=path, error=exc)) from exc


def jdbc_address(url: str) -> tuple[str, str, str, dict[str, str]]:
    """Hosts, port, database and parameters of a pgjdbc URL, the hosts as a libpq list when there are several.

    jdbc:postgresql://h1:6544,h2:6545/orders?sslmode=require: a host without a port of its own takes 5432,
    as in pgjdbc, and the database and the parameters are decoded as pgjdbc decodes them. A URL that names
    no host gives none: the import does not guess one.
    """
    match = JDBC_URL.fullmatch(url.strip())
    if match is None:
        return "", "5432", "", {}
    pairs = _endpoints(match.group(1), "5432")
    host, port = _hosts_and_ports(pairs) if pairs else ("", "5432")
    return host, port, unquote_plus(match.group(2)), dict(parse_qsl(match.group(3)))


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
    """The stand the pattern finds in the folder, the connection name or the host, in that order.

    Lower case, without the separator a name may put between the letters and the number: DEV-27 is the
    stand dev27, so that it folds into a range with dev26 and dev28.
    """
    for text in (connection.folder, connection.name, connection.host):
        match = pattern.search(text)
        if match:
            found = match.group("stand") if "stand" in pattern.groupindex else match.group(0)
            return _SEPARATORS.sub("", found.lower())
    return None


def stand_from_name(name: str) -> str | None:
    """The connection name as the stand, for a connection the pattern says nothing about: IFT-Caldera is ift-caldera.

    Lower case, the words joined by "-", only what a section name may hold; None when no Latin letter is left.
    """
    words = _NAME_WORD.findall(name.lower())
    return "-".join(words) if any(char.isalpha() for word in words for char in word) else None


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
        from_name = stand is None
        if from_name:
            stand = stand_from_name(connection.name)
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
        if from_name:
            result.named.append(target)
        if connection.password is None:
            result.without_password.append(target)
        else:
            for host, port in connection.endpoints:  # libpq matches a password line against one host
                result.passwords[host, port, connection.database, connection.user] = connection.password
    return result


def _problem(connection: Connection, stand: str | None) -> str | None:
    if connection.tunnel is not None:  # its host is the database's as the SSH server sees it, often localhost
        return t("import.ssh_tunnel", server=connection.tunnel) if connection.tunnel else t("import.ssh_tunnel_unnamed")
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
        notes = [t("import.preview_named")] if target in result.named else []
        if target in result.without_password:
            notes.append(t("import.preview_no_password"))
        lines.append(f"  [{target.name}]  {target.address}" + "".join(f"  {note}" for note in notes))
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
