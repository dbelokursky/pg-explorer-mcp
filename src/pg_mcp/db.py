"""Read-only access to PostgreSQL through the vendored pg8000 driver.

Connection settings come from the PG* environment variables and the password
file that psql uses, so a user can check access with psql and the server picks
up the same configuration. Failures are raised with a message that tells the
model, and through it the user, what to fix.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import socket
import ssl
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pg8000.exceptions
import pg8000.native

from .i18n import t

SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_STATEMENT_TIMEOUT_MS = 5000
SOCKET_TIMEOUT_S = 30
STATEMENT_GRACE_S = 5  # a socket outwaits the statement timeout, so PostgreSQL's own error comes first
CURSOR_NAME = "pg_mcp_rows"


class DatabaseUnavailableError(Exception):
    """The server cannot reach or log in to the database."""


class QueryFailedError(Exception):
    """PostgreSQL rejected a statement."""


@dataclass
class Counters:
    """Statements run through read_only and the rows they returned, for the query log."""

    statements: int = 0
    rows: int = 0


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database: str
    user: str
    password: str | None
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
        host = env.get("PGHOST") or "localhost"
        port = _int_setting(env, "PGPORT", 5432)
        database = env.get("PGDATABASE") or user
        sslmode = (env.get("PGSSLMODE") or "prefer").lower()
        if sslmode not in SSL_MODES:
            raise ValueError(t("db.bad_sslmode", value=sslmode, choices=", ".join(SSL_MODES)))
        passfile = pgpass_path(env)
        return cls(
            host=host,
            port=port,
            database=database,
            user=user,
            password=env.get("PGPASSWORD") or read_pgpass(passfile, host, port, database, user),
            sslmode=sslmode,
            sslrootcert=env.get("PGSSLROOTCERT") or None,
            statement_timeout_ms=_int_setting(env, "PG_MCP_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS),
            passfile=passfile,
            socket_timeout_s=_int_setting(env, "PG_MCP_SOCKET_TIMEOUT_S", SOCKET_TIMEOUT_S),
            search_path=env.get("PG_MCP_SEARCH_PATH") or None,
        )

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database} (sslmode={self.sslmode})"


class Database:
    """One lazily opened, read-only session that reconnects once if it drops."""

    def __init__(self, env: Mapping[str, str] | None = None, counters: Counters | None = None) -> None:
        self._conn: pg8000.native.Connection | None = None
        self.settings: Settings | None = None
        self._config_error: str | None = None
        self._counters = counters or Counters()  # shared by all connections of a stands pool
        try:
            self.settings = Settings.from_env(os.environ if env is None else env)
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
        """Run exactly one statement; :name placeholders take values from params."""
        for attempt in (1, 2):
            conn = self._connection()
            try:
                return _run_one_statement(conn, sql, params)
            except pg8000.exceptions.DatabaseError as exc:
                raise QueryFailedError(_server_message(exc)) from exc
            except (pg8000.exceptions.InterfaceError, OSError) as exc:
                self.close()
                if attempt == 2:
                    raise DatabaseUnavailableError(t("db.lost", target=self.describe_target(), error=exc)) from exc
        raise AssertionError("unreachable")

    def read_only(self, sql: str, max_rows: int, *, cursor: bool) -> tuple[list[str], list[Sequence[Any]], bool]:
        """Run one statement inside a READ ONLY transaction that is always rolled back.

        The text goes to PostgreSQL as is, through the extended protocol, so it can
        hold one statement only, and pg8000 never parses it for :name parameters.
        With cursor=True the rows come through a cursor, never more than max_rows + 1,
        to learn whether more exist. Returns column names, rows and that flag.
        """
        for attempt in (1, 2):
            conn = self._connection()
            try:
                conn.execute_simple("BEGIN TRANSACTION READ ONLY")
                try:
                    if cursor:
                        conn.execute_unnamed(f"DECLARE {CURSOR_NAME} NO SCROLL CURSOR FOR {sql}")
                        context = conn.execute_simple(f"FETCH FORWARD {int(max_rows) + 1} FROM {CURSOR_NAME}")
                    else:
                        context = conn.execute_unnamed(sql)
                finally:
                    conn.execute_simple("ROLLBACK")
            except pg8000.exceptions.DatabaseError as exc:
                raise QueryFailedError(_server_message(exc)) from exc
            except (pg8000.exceptions.InterfaceError, OSError) as exc:
                self.close()
                if attempt == 2:
                    raise DatabaseUnavailableError(t("db.lost", target=self.describe_target(), error=exc)) from exc
                continue
            columns = [str(column["name"]) for column in context.columns or []]
            rows = list(context.rows or [])
            returned = rows[:max_rows]
            self._counters.statements += 1
            self._counters.rows += len(returned)
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

    def _connection(self) -> pg8000.native.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> pg8000.native.Connection:
        settings = self.settings
        if settings is None:
            raise DatabaseUnavailableError(t("db.settings_invalid", error=self._config_error))
        socket_dir = settings.host if settings.host.startswith("/") else None
        attempts = _ssl_attempts(settings)
        for number, ssl_context in enumerate(attempts, start=1):
            try:
                conn = pg8000.native.Connection(
                    user=settings.user,
                    password=settings.password,
                    host=None if socket_dir else settings.host,
                    port=settings.port,
                    database=settings.database,
                    unix_sock=str(Path(socket_dir) / f".s.PGSQL.{settings.port}") if socket_dir else None,
                    ssl_context=ssl_context,
                    timeout=socket_timeout(settings),
                    application_name="pg-mcp",
                )
                break
            except pg8000.exceptions.Error as exc:
                if number == len(attempts):
                    raise DatabaseUnavailableError(_explain_connect_failure(exc, settings)) from exc
            except AttributeError as exc:
                # pg8000 1.31 reports a missing password for cleartext and MD5 logins,
                # but SCRAM fails on None.decode() instead.
                if settings.password is not None:
                    raise
                raise DatabaseUnavailableError(_password_hint(settings, t("db.password_unset"))) from exc
        try:
            # Guardrails, not the security boundary: the role's privileges are.
            conn.run("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            conn.run(f"SET statement_timeout = {int(settings.statement_timeout_ms)}")
            if settings.search_path:
                quoted = '"' + settings.search_path.replace('"', '""') + '"'
                conn.run(f"SET search_path = {quoted}, public")
        except pg8000.exceptions.Error as exc:
            conn.close()
            raise DatabaseUnavailableError(t("db.session_setup", error=exc)) from exc
        return conn


def _run_one_statement(conn: pg8000.native.Connection, sql: str, params: dict[str, Any]) -> list[Sequence[Any]]:
    # Always the extended query protocol, where PostgreSQL rejects a string with
    # several statements. pg8000's run() without parameters uses the simple
    # protocol instead, which executes "SELECT 1; COMMIT; DROP ..." in full.
    statement = conn.prepare(sql)
    try:
        rows: list[Sequence[Any]] = statement.run(**params)
    finally:
        statement.close()
    return rows


def socket_timeout(settings: Settings) -> int:
    """Seconds a socket may wait: past the statement timeout, so a slow statement fails with its own error.

    A socket that gave up first would drop the session, and query() would run the statement again.
    """
    configured = settings.socket_timeout_s if settings.socket_timeout_s > 0 else SOCKET_TIMEOUT_S
    return max(configured, settings.statement_timeout_ms // 1000 + STATEMENT_GRACE_S)


def pgpass_path(env: Mapping[str, str]) -> str:
    if env.get("PGPASSFILE"):
        return env["PGPASSFILE"]
    if sys.platform == "win32":
        return str(Path(env.get("APPDATA") or Path.home()) / "postgresql" / "pgpass.conf")
    return str(Path.home() / ".pgpass")


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


def _server_message(exc: Exception) -> str:
    fields = _error_fields(exc)
    if not fields:
        return str(exc)
    text = t("db.server_error", message=fields.get("M", "error"), code=fields.get("C", "?"))
    if fields.get("H"):
        text = t("db.server_hint", text=text, hint=fields["H"])
    return text


def _explain_connect_failure(exc: Exception, settings: Settings) -> str:
    cause = exc.__cause__
    fields = _error_fields(exc)
    code = fields.get("C")
    detail = fields.get("M") or str(exc)
    where = f"{settings.host}:{settings.port}"
    if isinstance(cause, (ConnectionRefusedError, FileNotFoundError)):
        return t("db.refused", where=where)
    if isinstance(cause, socket.gaierror):
        return t("db.unresolved", host=settings.host)
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return t("db.timeout", where=where)
    if code == "28P01" or (not settings.password and "password" in detail.lower()):
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
