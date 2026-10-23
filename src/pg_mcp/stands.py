"""Many stands in one server entry: the registry of connections and the pool that opens them.

The registry uses the pg_service.conf format of libpq: one section per
connection, named stand.database.schema, with host, port, dbname and user (the
schema's name when omitted); passwords stay in the password file. The server
only reads the registry, and reads it leniently: a missing file, an unreadable
file or a broken section becomes a warning in status, never a failure (ADR 0006).
"""

from __future__ import annotations

import difflib
import fnmatch
import re
import stat
import sys
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import DEFAULT_STATEMENT_TIMEOUT_MS, Counters, Database, Settings
from .i18n import plural, t

DEFAULT_MAX_CONNECTIONS = 8
DEFAULT_SOCKET_TIMEOUT_S = 10  # a dropped VPN should not hold the agent for long
SCHEMAS_LISTED = 15  # schemas an error names before it says how many more there are
MAX_STANDS_PER_CALL = 10  # stands one call may ask in turn: the client waits 30 seconds for an answer
_WILDCARDS = "*?["  # what makes an item of stands a pattern rather than a name
_NAME = re.compile(r"[A-Za-z0-9_$-]+")
_STAND_NUMBER = re.compile(r"(\D*)(\d*)(.*)")
_RANGE = "\u2013"  # an en dash between the first and the last stand of a run
_PASSWORD_LINE = re.compile(r"^\s*password\s*=", re.MULTILINE)
# install.py export writes this line above every section it puts into a service file: such a section
# is a copy of a stand for psql, not a stand of its own, and the next export may rewrite it.
EXPORTED = "# pg-mcp export"


class StandError(Exception):
    """The arguments do not name a stand or schema of the registry; the message says what does."""


@dataclass(frozen=True)
class Target:
    """One connection of the registry: a schema of a database on a stand."""

    stand: str
    database: str
    schema: str
    options: Mapping[str, str]
    source: str

    @property
    def name(self) -> str:
        return f"{self.stand}.{self.database}.{self.schema}"

    @property
    def label(self) -> str:
        """How answers name it: stand / database / schema."""
        return f"{self.stand} / {self.database} / {self.schema}"

    @property
    def address(self) -> str:
        """host:port, shared by the logins of one server."""
        return f"{self.options['host']}:{self.options.get('port', '5432')}"

    def env(self, base: Mapping[str, str]) -> dict[str, str]:
        """PG* variables for Database: the section's settings over what the server itself got."""
        # TLS settings and timeouts of the entry apply to every stand unless a section says otherwise;
        # the entry's own host, login and password never do.
        keep = (
            "PGPASSFILE",
            "APPDATA",
            "PGSSLMODE",
            "PGSSLROOTCERT",
            "PG_MCP_STATEMENT_TIMEOUT_MS",
            "PG_MCP_SOCKET_TIMEOUT_S",
        )
        env = {key: base[key] for key in keep if base.get(key)}
        env.setdefault("PG_MCP_SOCKET_TIMEOUT_S", str(DEFAULT_SOCKET_TIMEOUT_S))
        env["PGHOST"] = self.options["host"]
        env["PGPORT"] = self.options.get("port", "5432")
        env["PGDATABASE"] = self.options.get("dbname", self.database)
        env["PGUSER"] = self.options.get("user", self.schema)
        env["PG_MCP_SEARCH_PATH"] = self.schema
        optional = (
            ("sslmode", "PGSSLMODE"),
            ("sslrootcert", "PGSSLROOTCERT"),
            ("connect_timeout", "PG_MCP_SOCKET_TIMEOUT_S"),
            ("password", "PGPASSWORD"),
        )
        for option, variable in optional:
            if self.options.get(option):
                env[variable] = self.options[option]
        return env


@dataclass
class Registry:
    targets: list[Target] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)  # every file looked at, present or not
    state: dict[str, tuple[int, int] | None] = field(default_factory=dict)  # mtime and size per path
    patterns: list[str] = field(default_factory=list)  # the PG_MCP_STANDS patterns the stands were chosen by

    def changed(self) -> bool:
        """Whether any of the files looked at has changed since the registry was read."""
        return _state(self.paths) != self.state

    def stands(self) -> list[str]:
        return sorted({target.stand for target in self.targets}, key=_natural)

    def of_stand(self, stand: str) -> list[Target]:
        return [target for target in self.targets if target.stand == stand]

    def stand(self, name: Any, argument: str = "stand") -> str:
        """The registry's stand that name means, or StandError with similar names; argument names the parameter."""
        if not isinstance(name, str) or not name.strip():
            raise StandError(t("stands.no_stand_argument", argument=argument, stands=summarize(self.stands())))
        wanted = name.strip().lower()
        if wanted in self.stands():
            return wanted
        similar = difflib.get_close_matches(wanted, self.stands(), n=5, cutoff=0.6)
        if similar:
            raise StandError(t("stands.unknown_similar", stand=name, similar=", ".join(similar)))
        raise StandError(t("stands.unknown", stand=name, stands=summarize(self.stands())))

    def matching(self, raw: Any) -> list[str]:
        """The stands of names separated by commas or fnmatch patterns ("dev1,dev2", "dev*"), in stands() order.

        Names fold to lower case, as in stand(). More than MAX_STANDS_PER_CALL stands is an error
        that shows what matched: a call asks them one after another, and the client waits only so long.
        """
        items = raw if isinstance(raw, list) else raw.split(",") if isinstance(raw, str) else []
        wanted = [item.strip().lower() for item in items if isinstance(item, str) and item.strip()]
        if not wanted:
            raise StandError(t("stands.bad_stands_argument", stands=summarize(self.stands())))
        chosen: set[str] = set()
        for item in wanted:
            if any(char in item for char in _WILDCARDS):
                found = [stand for stand in self.stands() if fnmatch.fnmatchcase(stand, item)]
                if not found:
                    raise StandError(t("stands.no_match", pattern=item, stands=summarize(self.stands())))
                chosen.update(found)
            else:
                chosen.add(self.stand(item))
        stands = [stand for stand in self.stands() if stand in chosen]
        if len(stands) > MAX_STANDS_PER_CALL:
            patterns = ", ".join(wanted)
            raise StandError(
                plural(
                    "stands.too_many",
                    len(stands),
                    patterns=patterns,
                    stands=summarize(stands),
                    limit=MAX_STANDS_PER_CALL,
                )
            )
        return stands

    def target(self, stand: str, schema: Any, database: Any = None) -> Target:
        """The connection for a schema of a stand; database only matters when the schema repeats."""
        known = sorted({target.schema for target in self.of_stand(stand)})
        if not isinstance(schema, str) or not schema.strip():
            raise StandError(t("stands.no_schema_argument", stand=stand, schemas=listed(known)))
        wanted = schema.strip().strip('"').lower()
        matches = [target for target in self.of_stand(stand) if target.schema.lower() == wanted]
        if isinstance(database, str) and database.strip():
            chosen = [target for target in matches if target.database.lower() == database.strip().lower()]
            if matches and not chosen:
                databases = ", ".join(sorted(target.database for target in matches))
                raise StandError(
                    t("stands.schema_elsewhere", stand=stand, schema=schema, database=database, databases=databases)
                )
            matches = chosen
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            databases = ", ".join(sorted(target.database for target in matches))
            raise StandError(t("stands.schema_in_databases", stand=stand, schema=schema, databases=databases))
        similar = difflib.get_close_matches(wanted, known, n=5, cutoff=0.6)
        if similar:
            raise StandError(t("stands.unknown_schema_similar", stand=stand, schema=schema, similar=", ".join(similar)))
        raise StandError(t("stands.unknown_schema", stand=stand, schema=schema, schemas=listed(known)))


def listed(names: Sequence[str], limit: int = SCHEMAS_LISTED) -> str:
    """Names for an error message: all of them, or the first ones and how many there are in all."""
    if len(names) <= limit:
        return ", ".join(names) or t("error.none")
    return t("stands.more_names", names=", ".join(names[:limit]), count=len(names))


def stands_enabled(env: Mapping[str, str]) -> list[str] | None:
    """The stand name patterns PG_MCP_STANDS asks for, or None when stands are off."""
    raw = env.get("PG_MCP_STANDS", "").strip()
    if not raw or raw.lower() in ("off", "no", "false", "0"):
        return None
    return [pattern.strip().lower() for pattern in raw.split(",") if pattern.strip()]


def registry_files(env: Mapping[str, str]) -> list[Path]:
    """The stands file the import writes, then the user's own pg_service.conf."""
    home = Path(env["APPDATA"]) if sys.platform == "win32" and env.get("APPDATA") else Path.home()
    folder = "pg-mcp" if sys.platform == "win32" else ".pg-mcp"
    own = env.get("PG_MCP_STANDS_FILE") or str(home / folder / "stands.conf")
    if env.get("PGSERVICEFILE"):
        service = env["PGSERVICEFILE"]
    elif sys.platform == "win32":
        service = str(home / "postgresql" / ".pg_service.conf")
    else:
        service = str(home / ".pg_service.conf")
    return [Path(own).expanduser(), Path(service).expanduser()]


def load_registry(
    env: Mapping[str, str], files: Sequence[Path] | None = None, patterns: Sequence[str] | None = None
) -> Registry | None:
    """Read the stands; None when PG_MCP_STANDS is off. Never raises: problems become warnings."""
    patterns = stands_enabled(env) if patterns is None else list(patterns)
    if patterns is None:
        return None
    paths = list(registry_files(env) if files is None else files)
    registry = Registry(paths=paths, state=_state(paths), patterns=list(patterns))
    seen: set[str] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            registry.warnings.append(t("stands.unreadable", path=path, error=exc))
            continue
        registry.files.append(str(path))
        if _PASSWORD_LINE.search(text) and _readable_by_others(path):
            registry.warnings.append(t("stands.readable_by_others", path=path))
        sections, problems = parse_service_file(text)
        registry.warnings.extend(t("stands.bad_line", path=path, problem=problem) for problem in problems)
        copies = exported_sections(text)
        for name, options in sections.items():
            if name in copies:
                continue  # the stand itself is in another file, or was removed from it
            target = _target(name, options, str(path), registry.warnings)
            if target is None or target.name in seen:
                continue
            if any(fnmatch.fnmatchcase(target.stand, pattern) for pattern in patterns):
                seen.add(target.name)
                registry.targets.append(target)
    if not registry.targets:
        where = ", ".join(str(path) for path in (registry_files(env) if files is None else files))
        registry.warnings.append(t("stands.none_found", files=where))
    return registry


def _state(paths: Sequence[Path]) -> dict[str, tuple[int, int] | None]:
    return {str(path): _stamp(path) for path in paths}


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_mtime_ns, found.st_size


def _readable_by_others(path: Path) -> bool:
    if sys.platform == "win32":
        return False  # no mode bits to speak of; the file lives under the user's profile
    try:
        return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    except OSError:
        return False


def parse_service_file(text: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Sections of a pg_service.conf file and the lines that could not be read."""
    sections: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    current: dict[str, str] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip(), {})
        elif "=" in line and current is not None:
            key, _, value = line.partition("=")
            current[key.strip().lower()] = value.strip()
        else:
            problems.append(t("stands.line", number=number))
    return sections, problems


def exported_sections(text: str) -> set[str]:
    """The sections of a service file that install.py export wrote: the EXPORTED line is right above them."""
    found: set[str] = set()
    previous = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]") and previous.startswith(EXPORTED):
            found.add(line[1:-1].strip())
        previous = line
    return found


class Pool:
    """Opens the registry's connections on demand and keeps only the most recently used ones."""

    def __init__(self, registry: Registry, env: Mapping[str, str], limit: int = DEFAULT_MAX_CONNECTIONS) -> None:
        self.registry = registry
        self._env = env
        self._limit = max(1, limit)
        self._open: OrderedDict[str, Database] = OrderedDict()
        self._counters = Counters()
        self.current: Target | None = None
        self.outcomes: dict[str, str] = {}  # stand -> how a call over several stands ended there

    def database(self, target: Target) -> Database:
        self.current = target
        database = self._open.pop(target.name, None) or Database(target.env(self._env), counters=self._counters)
        self._open[target.name] = database
        while len(self._open) > self._limit:
            _, oldest = self._open.popitem(last=False)
            oldest.close()
        return database

    @property
    def limit(self) -> int:
        return self._limit

    def open_count(self) -> int:
        return len(self._open)

    def close(self) -> None:
        for database in self._open.values():
            database.close()
        self._open.clear()

    def refresh(self) -> bool:
        """Re-read the registry when its files changed, so an import shows up without a restart.

        A change that leaves no stands readable keeps the stands read before, with a warning.
        """
        if not self.registry.changed():
            return False
        fresh = load_registry(self._env, self.registry.paths, self.registry.patterns)
        if fresh is not None and fresh.targets:
            self.registry = fresh
            return True
        self.registry.state = _state(self.registry.paths)  # noted: no point re-reading until the next change
        kept = [
            *(fresh.warnings if fresh else []),
            t("stands.reload_kept", files=", ".join(map(str, self.registry.paths))),
        ]
        self.registry.warnings = kept
        return False

    # The query log reads these, as it does from a single Database.
    @property
    def statements_run(self) -> int:
        return self._counters.statements

    @property
    def rows_read(self) -> int:
        return self._counters.rows

    @property
    def settings(self) -> Settings | None:
        database = self._open.get(self.current.name) if self.current else None
        return database.settings if database else None

    def log_context(self) -> dict[str, Any]:
        if self.outcomes:
            return {"stands": dict(self.outcomes)}  # a call over several stands: how it ended on each
        if self.current is None:
            return {}
        return {"stand": self.current.stand, "schema": self.current.schema}

    def forget(self) -> None:
        """Forget the previous call, so that the query log describes this one only."""
        self.current = None
        self.outcomes.clear()

    def connect_timeout(self, target: Target) -> int:
        """Seconds a connection to the target may wait for an answer: the section's, else the entry's."""
        raw = target.env(self._env)["PG_MCP_SOCKET_TIMEOUT_S"]
        return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_SOCKET_TIMEOUT_S

    def statement_timeout(self, target: Target) -> int:
        """Seconds a statement on the target may run before PostgreSQL stops it, rounded up."""
        raw = target.env(self._env).get("PG_MCP_STATEMENT_TIMEOUT_MS", "")
        milliseconds = int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_STATEMENT_TIMEOUT_MS
        return -(-milliseconds // 1000)


def summarize(stands: Sequence[str]) -> str:
    """Stand names with numbered runs folded into ranges, so fifty dev stands take one entry."""
    groups: dict[str, list[int]] = {}
    others: list[str] = []
    for name in stands:
        prefix, digits, rest = _STAND_NUMBER.fullmatch(name).groups()  # type: ignore[union-attr]
        if digits and not rest:
            groups.setdefault(prefix, []).append(int(digits))
        else:
            others.append(name)
    parts = []
    for prefix, numbers in groups.items():
        numbers.sort()
        start = previous = numbers[0]
        for number in [*numbers[1:], None]:
            if number is not None and number == previous + 1:
                previous = number
                continue
            parts.append(f"{prefix}{start}" if start == previous else f"{prefix}{start}{_RANGE}{prefix}{previous}")
            if number is not None:
                start = previous = number
    return ", ".join([*parts, *others]) or t("error.none")


def _target(name: str, options: dict[str, str], source: str, warnings: list[str]) -> Target | None:
    parts = name.split(".")
    if len(parts) != 3 or not all(_NAME.fullmatch(part) for part in parts):
        return None  # an ordinary psql service, not one of ours
    if not options.get("host"):
        warnings.append(t("stands.no_host", name=name, path=source))
        return None
    for option in ("port", "connect_timeout"):
        if option in options and not options[option].isdigit():
            warnings.append(t("stands.bad_number", name=name, path=source, option=option, value=options[option]))
            return None
    stand, database, schema = parts
    return Target(stand.lower(), database, schema, dict(options), source)


def _natural(name: str) -> tuple[str, int, str]:
    prefix, digits, rest = _STAND_NUMBER.fullmatch(name).groups()  # type: ignore[union-attr]
    return prefix, int(digits) if digits else -1, rest
