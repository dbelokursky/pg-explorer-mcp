"""Import of stand connections from DBeaver, DataGrip or pg_service.conf into the stands registry (ADR 0006).

The import never runs inside the server: install.py calls it, previews by default and writes
only on request. Writing is all or nothing as far as two files allow: both are prepared in
full before either is put in place, each gets a backup, and the server keeps working with
whatever it had if anything fails. Passwords never reach the screen.
"""

from __future__ import annotations

import datetime
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus

from . import passwords
from .aes import decrypt_cbc
from .clients import app_data
from .db import split_hosts
from .i18n import t
from .stands import Target, export_uuid, exported_sections, parse_service_file
from .user_files import discard, place, stage, unplace

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
        """host:port of every host, as the preview shows them."""
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
    """The stand pattern, case-insensitive; None when it does not compile."""
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


def preview(result: Plan, registry_file: Path, store: passwords.Passwords | None) -> list[str]:
    """Lines for the screen: what goes where, without a single password."""
    lines = [t("import.preview_header", count=len(result.targets), registry=registry_file)]
    for target in result.targets:
        notes = [t("import.preview_named")] if target in result.named else []
        if target in result.without_password:
            notes.append(t("import.preview_no_password", command=_password_command(result.targets, target)))
        lines.append(f"  [{target.name}]  {target.address}" + "".join(f"  {note}" for note in notes))
    if result.passwords:
        where = store.label if store is not None else t("error.none")
        lines.append(t("import.preview_passwords", count=len(result.passwords), where=where))
    if result.skipped:
        lines.append(t("import.preview_skipped", count=len(result.skipped)))
        lines.extend(f"  {connection.source}: {connection.name}  {reason}" for connection, reason in result.skipped)
    return lines


def _password_command(targets: Sequence[Target], target: Target) -> str:
    """The command that puts the password of this login into the store, by stand and schema.

    With --database when another database of the stand has a section of the same schema name.
    """
    command = f"python3 install.py password --stand {target.stand} --schema {target.schema}"
    same_schema = {other.database for other in targets if other.stand == target.stand and other.schema == target.schema}
    return f"{command} --database {target.database}" if len(same_schema) > 1 else command


# --- writing -------------------------------------------------------------------------------------


def write(result: Plan, registry_file: Path, store: passwords.Passwords | None, *, prune: bool = False) -> list[str]:
    """Put the registry in its place and the passwords into the password store; on a failure nothing changes.

    The registry is prepared in full before anything is placed, and placed first: when the passwords
    cannot be kept, it is taken back. Where the system has no password store, an import that has
    passwords is refused: they would go nowhere (ADR 0012).
    """
    if not result.targets:
        raise ImportRefusedError(t("import.nothing"))
    if result.passwords and store is None:
        raise ImportRefusedError(t("import.no_store"))
    registry_text = _registry_text(registry_file, result.targets, prune=prune)
    try:
        staged = stage(registry_file, registry_text)
    except OSError as exc:
        raise ImportRefusedError(t("import.cannot_write", path=exc.filename or registry_file, error=exc)) from exc
    try:
        backup = place(staged, owner_only=True)
    except OSError as exc:
        discard([staged])
        raise ImportRefusedError(t("import.cannot_place", path=registry_file, error=exc)) from exc
    lines = [t("import.written", path=registry_file)]
    if backup is not None:
        lines = [t("import.written_backup", path=registry_file, backup=backup)]
    if result.passwords and store is not None:
        try:
            store.put(result.passwords)
        except passwords.PasswordStoreError as exc:
            unplace(staged, backup)  # a registry whose stands have no passwords would mislead: back as it was
            raise ImportRefusedError(t("import.passwords_not_stored", error=exc)) from exc
        lines.append(t("import.passwords_stored", count=len(result.passwords), store=store.label))
    return lines


def _registry_text(registry_file: Path, targets: Sequence[Target], *, prune: bool) -> str:
    sections: dict[str, dict[str, str]] = {}
    if not prune:
        try:
            sections, _ = parse_service_file(registry_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError):
            pass  # no registry yet, not even its folder: there is nothing to keep
        except (OSError, UnicodeDecodeError) as exc:
            # Counted as empty, the registry would be replaced by the imported stands alone.
            raise ImportRefusedError(t("import.unreadable", path=registry_file, error=exc)) from exc
    for target in targets:
        sections[target.name] = dict(target.options)
    when = datetime.date.today().isoformat()
    out = [
        f"# Stands of pg-explorer-mcp, written by install.py import on {when}. Sections are stand.database.schema;",
        "# the server reads this file when it starts. Passwords belong in the password store.",
        "",
    ]
    for name in sorted(sections, key=_natural_key):
        out.append(f"[{name}]")
        out.extend(f"{key}={value}" for key, value in sections[name].items())
        out.append("")
    return "\n".join(out)


def _natural_key(name: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]
