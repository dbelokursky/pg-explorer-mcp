"""Read-only access to PostgreSQL through the vendored pg8000 driver.

Connection settings come from the PG* environment variables and the password
file that psql uses, so a user can check access with psql and the server picks
up the same configuration. PGHOST may list several hosts separated by commas,
with one port for all of them or one per host in PGPORT, and they are tried in
turn, as libpq tries them. Failures are raised with a message that tells the
model, and through it the user, what to fix.
"""

from __future__ import annotations

import contextlib
import errno
import getpass
import math
import os
import socket
import ssl
import stat
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pg8000.exceptions
import pg8000.native

from . import environment
from .i18n import t

# No route to the host: with the stands behind a VPN, the usual sign that the VPN is down.
_NO_ROUTE = frozenset(
    code for code in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN, getattr(errno, "EHOSTDOWN", None)) if code
)
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_STATEMENT_TIMEOUT_MS = 5000
SOCKET_TIMEOUT_S = 30
STATEMENT_GRACE_S = 5  # a socket outwaits the statement timeout, so PostgreSQL's own error comes first
CURSOR_NAME = "pg_mcp_rows"


class DatabaseUnavailableError(Exception):
    """The server cannot reach or log in to the database.

    host_down says the host did not answer at all (refused, unresolved, unreachable,
    timed out, or the connection was lost), as opposed to a server that answered and
    turned the login down: another login on that host would fail the same way.
    """

    def __init__(self, message: str, *, host_down: bool = False) -> None:
        super().__init__(message)
        self.host_down = host_down


class QueryFailedError(Exception):
    """PostgreSQL rejected a statement: code is its SQLSTATE and server_message its own text, when it sent them."""

    def __init__(self, text: str, code: str | None = None, server_message: str | None = None) -> None:
        super().__init__(text)
        self.code = code
        self.server_message = server_message


class OverdueError(Exception):
    """The call this thread works for has answered without it, so nothing more is done for that call."""


# For a thread that asks one stand of a call over several, when that call answers (time.monotonic());
# None in the call's own thread. The call answers then without the stands still at work (ADR 0006).
_deadline: ContextVar[float | None] = ContextVar("pg_mcp_deadline", default=None)


def answer_by(deadline: float) -> None:
    """Work for a call that answers by this time (time.monotonic()), from now on in the current context.

    Past it the call has answered without this thread, which may still be running a statement: the thread
    takes no connection and starts no statement, a wait for a busy session ends, and rows that arrive
    later count for no call.
    """
    _deadline.set(deadline)


def deadline() -> float | None:
    """When the call this context works for answers; None in the call's own thread."""
    return _deadline.get()


def overdue() -> bool:
    """Whether the call this context works for has answered already, without this thread."""
    due = _deadline.get()
    return due is not None and time.monotonic() >= due


@dataclass
class Counters:
    """Statements run through read_only and the rows they returned, for the query log.

    A call over several stands runs its statements in threads, so the counts are bumped under a lock.
    """

    statements: int = 0
    rows: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def count(self, rows: int) -> None:
        with self._lock:
            self.statements += 1
            self.rows += rows


@dataclass(frozen=True)
class Endpoint:
    """One host of the settings, with its port and the password the password file holds for that host."""

    host: str
    port: int
    password: str | None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Settings:
    endpoints: tuple[Endpoint, ...]  # the hosts in the order to try them; at least one
    database: str
    user: str
    sslmode: str
    sslrootcert: str | None
    statement_timeout_ms: int
    passfile: str
    socket_timeout_s: int = SOCKET_TIMEOUT_S
    search_path: str | None = None  # a schema to look in first, for one of many stands

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        """Read libpq-style settings; ValueError explains a bad value."""
        user = env.get("PGUSER") or getpass.getuser()
        database = env.get("PGDATABASE") or user
        sslmode = (env.get("PGSSLMODE") or "prefer").lower()
        if sslmode not in SSL_MODES:
            raise ValueError(t("db.bad_sslmode", value=sslmode, choices=", ".join(SSL_MODES)))
        passfile = pgpass_path(env)
        password = env.get("PGPASSWORD")
        # The password file is read per host, as libpq reads it: each host may have a line of its own.
        endpoints = tuple(
            Endpoint(host, port, password or read_pgpass(passfile, host, port, database, user))
            for host, port in split_hosts(env.get("PGHOST") or "localhost", env.get("PGPORT") or "5432")
        )
        return cls(
            endpoints=endpoints,
            database=database,
            user=user,
            sslmode=sslmode,
            sslrootcert=env.get("PGSSLROOTCERT") or None,
            statement_timeout_ms=_int_setting(
                env, "PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS
            ),
            passfile=passfile,
            socket_timeout_s=_int_setting(env, "PG_EXPLORER_MCP_SOCKET_TIMEOUT_S", SOCKET_TIMEOUT_S),
            search_path=env.get("PG_EXPLORER_MCP_SEARCH_PATH") or None,
        )

    @property
    def host(self) -> str:
        """The first host: the only one of most settings."""
        return self.endpoints[0].host

    @property
    def port(self) -> int:
        return self.endpoints[0].port

    @property
    def password(self) -> str | None:
        return self.endpoints[0].password

    @property
    def addresses(self) -> str:
        """host:port of every host, separated by commas."""
        return ",".join(endpoint.address for endpoint in self.endpoints)

    def describe(self) -> str:
        return f"{self.user}@{self.addresses}/{self.database} (sslmode={self.sslmode})"


class Database:
    """One lazily opened, read-only session that reconnects once if it drops.

    The session runs one transaction at a time. A call over several stands answers without a stand
    that is slow, and that stand's thread goes on with its statement: the next call that needs the
    session waits for the statement to end (ADR 0006).
    """

    def __init__(self, env: Mapping[str, str] | None = None, counters: Counters | None = None) -> None:
        self._conn: pg8000.native.Connection | None = None
        self.settings: Settings | None = None
        self._config_error: str | None = None
        self._counters = counters or Counters()  # shared by all connections of a stands pool
        self._lock = threading.Lock()  # held for a transaction, and for opening the session it runs in
        try:
            self.settings = Settings.from_env(environment.with_legacy_names(os.environ) if env is None else env)
        except ValueError as exc:
            self._config_error = str(exc)

    @property
    def statements_run(self) -> int:
        return self._counters.statements

    @property
    def rows_read(self) -> int:
        return self._counters.rows

    def describe_target(self) -> str:
        if self.settings is None:
            return t("db.not_configured", error=self._config_error)
        return self.settings.describe()

    def query(self, sql: str, **params: Any) -> list[Sequence[Any]]:
        """Run exactly one statement in a transaction of its own; :name placeholders take values from params."""
        with self._turn():
            for attempt in (1, 2):
                conn = self._connection()
                try:
                    conn.execute_simple(transaction_start(self.settings))
                    try:
                        return _run_one_statement(conn, sql, params)
                    finally:
                        conn.execute_simple("ROLLBACK")
                except pg8000.exceptions.DatabaseError as exc:
                    raise _query_failed(exc) from exc
                except (pg8000.exceptions.InterfaceError, OSError) as exc:
                    self.close()
                    if attempt == 2:
                        raise DatabaseUnavailableError(self._lost(exc), host_down=True) from exc
        raise AssertionError("unreachable")

    def read_only(self, sql: str, max_rows: int, *, cursor: bool) -> tuple[list[str], list[Sequence[Any]], bool]:
        """Run one statement inside a READ ONLY transaction that is always rolled back.

        The text goes to PostgreSQL as is, through the extended protocol, so it can
        hold one statement only, and pg8000 never parses it for :name parameters.
        With cursor=True the rows come through a cursor, never more than max_rows + 1,
        to learn whether more exist. Returns column names, rows and that flag.
        """
        with self._turn():
            for attempt in (1, 2):
                conn = self._connection()
                try:
                    conn.execute_simple(transaction_start(self.settings))
                    try:
                        if cursor:
                            conn.execute_unnamed(f"DECLARE {CURSOR_NAME} NO SCROLL CURSOR FOR {sql}")
                            context = conn.execute_simple(f"FETCH FORWARD {int(max_rows) + 1} FROM {CURSOR_NAME}")
                        else:
                            context = conn.execute_unnamed(sql)
                    finally:
                        conn.execute_simple("ROLLBACK")
                except pg8000.exceptions.DatabaseError as exc:
                    raise _query_failed(exc) from exc
                except (pg8000.exceptions.InterfaceError, OSError) as exc:
                    self.close()
                    if attempt == 2:
                        raise DatabaseUnavailableError(self._lost(exc), host_down=True) from exc
                    continue
                columns = [str(column["name"]) for column in context.columns or []]
                rows = list(context.rows or [])
                returned = rows[:max_rows]
                if not overdue():  # rows that arrive after the call has answered count for no call
                    self._counters.count(len(returned))
                return columns, returned, len(rows) > max_rows
        raise AssertionError("unreachable")

    def server_info(self) -> dict[str, str]:
        row = self.query(
            "SELECT current_database(), current_user, current_setting('server_version'), "
            "current_setting('transaction_read_only'), current_setting('statement_timeout')"
        )[0]
        keys = ("database", "user", "server_version", "read_only", "statement_timeout")
        return {key: str(value) for key, value in zip(keys, row)}

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):  # the session is gone either way
                self._conn.close()
            self._conn = None

    def _lost(self, exc: Exception) -> str:
        return t("db.lost", target=self.describe_target(), error=exc)

    @contextlib.contextmanager
    def _turn(self) -> Iterator[None]:
        """The session for one transaction, once the transaction another thread runs on it has ended.

        That thread can only be a stand's that did not answer in time: its call answered without it, and
        it is finishing its statement. The wait is no longer than an open socket waits for an answer, past
        the statement timeout, and not past the end of the waiting thread's own call; a session busy for
        longer than the socket waits is reported as such. A wait that the end of the call cuts short ends
        with the call, though on Windows it may end a few milliseconds before time.monotonic() gets there:
        the thread gives up, and the call names the stand among those that did not answer in time. The
        session is opened in the turn too, so two threads never open two.
        """
        if overdue():
            raise OverdueError
        wait: float = socket_timeout(self.settings) if self.settings else SOCKET_TIMEOUT_S
        due = deadline()
        left = math.inf if due is None else due - time.monotonic()  # until the call answers
        if not self._lock.acquire(timeout=max(0.0, min(wait, left))):
            if left <= wait or overdue():
                raise OverdueError
            raise DatabaseUnavailableError(t("db.busy", target=self.describe_target()))
        try:
            yield
        finally:
            self._lock.release()

    def _connection(self) -> pg8000.native.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> pg8000.native.Connection:
        settings = self.settings
        if settings is None:
            raise DatabaseUnavailableError(t("db.settings_invalid", error=self._config_error))
        # The hosts in turn, as libpq tries them: the session is on the first that lets the login in.
        # The error names every host that did not, and host_down holds only when none of them answered.
        failures: list[DatabaseUnavailableError] = []
        for endpoint in settings.endpoints:
            try:
                conn = _open(settings, endpoint)
            except DatabaseUnavailableError as exc:
                failures.append(exc)
                continue
            return _prepared(conn, settings)
        every_host_down = all(failure.host_down for failure in failures)
        if len(failures) == 1:
            raise failures[0]
        message = t("db.every_host_failed", failures="; ".join(str(failure) for failure in failures))
        raise DatabaseUnavailableError(message, host_down=every_host_down) from failures[-1]


def _open(settings: Settings, endpoint: Endpoint) -> pg8000.native.Connection:
    """A connection to one host, or DatabaseUnavailableError with what went wrong there."""
    socket_dir = endpoint.host if endpoint.host.startswith("/") else None
    attempts = _ssl_attempts(settings)
    for number, ssl_context in enumerate(attempts, start=1):
        try:
            conn = pg8000.native.Connection(
                user=settings.user,
                password=endpoint.password,
                host=None if socket_dir else endpoint.host,
                port=endpoint.port,
                database=settings.database,
                unix_sock=str(Path(socket_dir) / f".s.PGSQL.{endpoint.port}") if socket_dir else None,
                ssl_context=ssl_context,
                timeout=connect_timeout(settings),
                application_name="pg-explorer-mcp",
            )
            break
        except (pg8000.exceptions.Error, OSError) as exc:
            # pg8000 wraps most socket errors, but not those of its TLS request.
            down = _host_down(exc)
            if down or number == len(attempts):  # a host that did not answer will not answer TLS either
                raise DatabaseUnavailableError(
                    _explain_connect_failure(exc, settings, endpoint), host_down=down
                ) from exc
        except AttributeError as exc:
            # pg8000 1.31 reports a missing password for cleartext and MD5 logins,
            # but SCRAM fails on None.decode() instead.
            if endpoint.password is not None:
                raise
            raise DatabaseUnavailableError(_password_hint(settings, t("db.password_unset"))) from exc
    return conn


def _prepared(conn: pg8000.native.Connection, settings: Settings) -> pg8000.native.Connection:
    """The connection with the session set up: read only, the statement timeout, the schema first."""
    # Connecting waited connect_timeout(); a statement may run for statement_timeout_ms,
    # so from here on the socket waits longer. pg8000 keeps the socket in _usock.
    conn._usock.settimeout(socket_timeout(settings))
    try:
        # Guardrails, not the security boundary: the role's privileges are. Every transaction sets
        # them again for itself (transaction_start), since a pooler may not keep the session.
        conn.run("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.run(f"SET statement_timeout = {int(settings.statement_timeout_ms)}")
        if settings.search_path:
            conn.run(f"SET search_path = {_quote_ident(settings.search_path)}, public")
    except pg8000.exceptions.Error as exc:
        conn.close()
        raise DatabaseUnavailableError(t("db.session_setup", error=exc)) from exc
    return conn


def _run_one_statement(conn: pg8000.native.Connection, sql: str, params: dict[str, Any]) -> list[Sequence[Any]]:
    # Always the extended query protocol, where PostgreSQL rejects a string with
    # several statements. pg8000's run() without parameters uses the simple
    # protocol instead, which executes "SELECT 1; COMMIT; DROP ..." in full.
    # The unnamed statement rather than a named one (prepare): a name is one more
    # thing a pooler can lose between the messages of a call.
    if params:
        rows: list[Sequence[Any]] = conn.run(sql, **params)
        return rows
    return list(conn.execute_unnamed(sql).rows or [])


def transaction_start(settings: Settings | None) -> str:
    """BEGIN with the limits of the call set for that transaction alone, as one message.

    A pooler in transaction mode between the server and PostgreSQL (pgbouncer, Odyssey) may serve each
    transaction from another server session, or reset the session in between (DISCARD ALL): what was
    SET when the connection was made is not there any more, and a prepared statement does not survive
    from one message to the next. So every call is one transaction, which pins one session for all its
    messages and carries its own limits.
    """
    statements = ["BEGIN TRANSACTION READ ONLY"]
    if settings is not None:
        statements.append(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
        if settings.search_path:
            statements.append(f"SET LOCAL search_path = {_quote_ident(settings.search_path)}, public")
    return "; ".join(statements)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_timeout(settings: Settings) -> int:
    """Seconds to wait for the server to answer a new connection."""
    return settings.socket_timeout_s if settings.socket_timeout_s > 0 else SOCKET_TIMEOUT_S


def socket_timeout(settings: Settings) -> int:
    """Seconds the connected socket may wait: past the statement timeout, so a slow statement
    fails with its own error. A socket that gave up first would drop the session, and query()
    would run the statement again.
    """
    return max(connect_timeout(settings), settings.statement_timeout_ms // 1000 + STATEMENT_GRACE_S)


def _host_down(exc: Exception) -> bool:
    """Whether the host did not answer at all, so that no login on it can succeed right now."""
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    if isinstance(cause, ssl.SSLError) or _dropped(exc):
        return False  # the host accepted the connection: it answered
    return isinstance(cause, OSError)


def _dropped(exc: Exception) -> bool:
    """Whether the server accepted the connection and then closed it before the login was through.

    pg8000 reports that as "network error", with or without the reset underneath. Windows
    discards what the server sent before a reset, so the server's own refusal, usually of a
    password, may arrive this way instead of as an error message.
    """
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    return (
        isinstance(cause, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))
        or str(exc) == "network error"
    )


def pgpass_path(env: Mapping[str, str]) -> str:
    if env.get("PGPASSFILE"):
        return env["PGPASSFILE"]
    if sys.platform == "win32":
        return str(Path(env.get("APPDATA") or Path.home()) / "postgresql" / "pgpass.conf")
    return str(Path.home() / ".pgpass")


def split_hosts(hosts: str, ports: str) -> list[tuple[str, int]]:
    """Hosts paired with their ports the way libpq pairs PGHOST and PGPORT.

    Both are comma-separated lists; a single port goes with every host, otherwise there is one per
    host. An empty host means localhost. ValueError explains a port that is not a number, or a count
    of ports that matches no host.
    """
    names = [name.strip() or "localhost" for name in hosts.split(",")]
    numbers = [number.strip() for number in ports.split(",")]
    if len(numbers) == 1:
        numbers = numbers * len(names)
    if len(numbers) != len(names):
        raise ValueError(t("db.ports_mismatch", value=ports, ports=len(numbers), hosts=len(names)))
    try:
        return [(name, int(number)) for name, number in zip(names, numbers)]
    except ValueError:
        raise ValueError(t("db.not_a_number", name="PGPORT", value=ports)) from None


def read_pgpass(path: str, host: str, port: int, database: str, user: str) -> str | None:
    """Look the password up the way libpq does: the first matching line wins, * matches anything."""
    passfile = Path(path)
    try:
        if sys.platform != "win32" and passfile.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None  # like libpq, ignore a password file that others can read
        lines = passfile.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    # libpq matches Unix-socket connections against "localhost".
    wanted = ("localhost" if host.startswith("/") else host, str(port), database, user)
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = _split_pgpass_line(line)
        if len(fields) == 5 and all(field in ("*", value) for field, value in zip(fields, wanted)):
            return fields[4]
    return None


def _split_pgpass_line(line: str) -> list[str]:
    # Fields are separated by ":"; "\:" and "\\" stand for a literal colon and backslash.
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def _ssl_attempts(settings: Settings) -> list[Any]:
    """ssl_context values for pg8000, in the order to try them.

    pg8000 reads False as plain TCP, None as TLS when the server offers it
    (certificate not checked) and True as TLS required (certificate not checked).
    """
    if settings.sslmode == "disable":
        return [False]
    if settings.sslmode == "allow":
        return [False, True]
    if settings.sslmode == "prefer":
        return [None]
    if settings.sslmode == "require":
        return [True]
    context = ssl.create_default_context(cafile=settings.sslrootcert)
    if settings.sslmode == "verify-ca":
        context.check_hostname = False
    return [context]


def _error_fields(exc: Exception) -> dict[str, str]:
    # pg8000 passes the server's error message fields (C = SQLSTATE, M = text, H = hint) as a dict.
    return exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}


def _query_failed(exc: Exception) -> QueryFailedError:
    fields = _error_fields(exc)
    return QueryFailedError(_server_message(exc), code=fields.get("C"), server_message=fields.get("M"))


def _server_message(exc: Exception) -> str:
    fields = _error_fields(exc)
    if not fields:
        return str(exc)
    text = t("db.server_error", message=fields.get("M", "error"), code=fields.get("C", "?"))
    if fields.get("H"):
        text = t("db.server_hint", text=text, hint=fields["H"])
    return text


def _explain_connect_failure(exc: Exception, settings: Settings, endpoint: Endpoint) -> str:
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    fields = _error_fields(exc)
    code = fields.get("C")
    detail = fields.get("M") or str(exc)
    where = endpoint.address
    if isinstance(cause, (ConnectionRefusedError, FileNotFoundError)):
        return t("db.refused", where=where)
    if isinstance(cause, socket.gaierror):
        return t("db.unresolved", host=endpoint.host)
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return t("db.timeout", where=where)
    if isinstance(cause, OSError) and cause.errno in _NO_ROUTE:
        return t("db.unreachable", where=where, error=cause.strerror or cause)
    if _dropped(exc):
        return t("db.dropped", where=where, user=settings.user)
    if code == "28P01" or (not endpoint.password and "password" in detail.lower()):
        return _password_hint(settings, detail)
    if code == "3D000":
        return t("db.no_database", database=settings.database, where=where)
    if code == "28000":
        return t("db.login_refused", user=settings.user, detail=detail)
    if isinstance(cause, ssl.SSLError) or "SSL" in detail:
        return t("db.tls", where=where, detail=cause or detail)
    return t("db.cannot_connect", target=settings.describe(), detail=detail)


def _password_hint(settings: Settings, detail: str) -> str:
    return t("db.password", user=settings.user, detail=detail, passfile=settings.passfile)


def _int_setting(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(t("db.not_a_number", name=name, value=raw)) from None
