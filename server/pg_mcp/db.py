"""Read-only access to PostgreSQL through the vendored pg8000 driver.

Connection settings come from the PG* environment variables and the password
file that psql uses, so a user can check access with psql and the server picks
up the same configuration. Failures are raised with a message that tells the
model, and through it the user, what to fix.
"""
from __future__ import annotations

import getpass
import os
import socket
import ssl
import stat
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pg8000.exceptions
import pg8000.native

SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_STATEMENT_TIMEOUT_MS = 5000
SOCKET_TIMEOUT_S = 30
CURSOR_NAME = "pg_mcp_rows"


class DatabaseUnavailable(Exception):
    """The server cannot reach or log in to the database."""


class QueryFailed(Exception):
    """PostgreSQL rejected a statement."""


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database: str
    user: str
    password: Optional[str]
    sslmode: str
    sslrootcert: Optional[str]
    statement_timeout_ms: int
    passfile: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """Read libpq-style settings; ValueError explains a bad value."""
        user = env.get("PGUSER") or getpass.getuser()
        host = env.get("PGHOST") or "localhost"
        port = _int_setting(env, "PGPORT", 5432)
        database = env.get("PGDATABASE") or user
        sslmode = (env.get("PGSSLMODE") or "prefer").lower()
        if sslmode not in SSL_MODES:
            raise ValueError(f"PGSSLMODE={sslmode!r} is not one of {', '.join(SSL_MODES)}")
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
        )

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database} (sslmode={self.sslmode})"


class Database:
    """One lazily opened, read-only session that reconnects once if it drops."""

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self._conn: Optional[pg8000.native.Connection] = None
        self.settings: Optional[Settings] = None
        self._config_error: Optional[str] = None
        try:
            self.settings = Settings.from_env(os.environ if env is None else env)
        except ValueError as exc:
            self._config_error = str(exc)

    def describe_target(self) -> str:
        if self.settings is None:
            return f"not configured: {self._config_error}"
        return self.settings.describe()

    def query(self, sql: str, **params: Any) -> List[Sequence[Any]]:
        """Run exactly one statement; :name placeholders take values from params."""
        for attempt in (1, 2):
            conn = self._connection()
            try:
                return _run_one_statement(conn, sql, params)
            except pg8000.exceptions.DatabaseError as exc:
                raise QueryFailed(_server_message(exc)) from exc
            except (pg8000.exceptions.InterfaceError, OSError) as exc:
                self.close()
                if attempt == 2:
                    raise DatabaseUnavailable(f"Lost the connection to {self.describe_target()}: {exc}") from exc
        raise AssertionError("unreachable")

    def read_only(self, sql: str, max_rows: int, cursor: bool) -> Tuple[List[str], List[Sequence[Any]], bool]:
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
                raise QueryFailed(_server_message(exc)) from exc
            except (pg8000.exceptions.InterfaceError, OSError) as exc:
                self.close()
                if attempt == 2:
                    raise DatabaseUnavailable(f"Lost the connection to {self.describe_target()}: {exc}") from exc
                continue
            columns = [str(column["name"]) for column in context.columns or []]
            rows = list(context.rows or [])
            return columns, rows[:max_rows], len(rows) > max_rows
        raise AssertionError("unreachable")

    def server_info(self) -> Dict[str, str]:
        row = self.query(
            "SELECT current_database(), current_user, current_setting('server_version'), "
            "current_setting('transaction_read_only'), current_setting('statement_timeout')"
        )[0]
        keys = ("database", "user", "server_version", "read_only", "statement_timeout")
        return {key: str(value) for key, value in zip(keys, row)}

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass  # the session is gone either way
            self._conn = None

    def _connection(self) -> pg8000.native.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> pg8000.native.Connection:
        settings = self.settings
        if settings is None:
            raise DatabaseUnavailable(
                f"Connection settings are invalid: {self._config_error}. "
                "Fix the env block of the pg server entry in the agent settings."
            )
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
                    unix_sock=os.path.join(socket_dir, f".s.PGSQL.{settings.port}") if socket_dir else None,
                    ssl_context=ssl_context,
                    timeout=SOCKET_TIMEOUT_S,
                    application_name="pg-mcp",
                )
                break
            except pg8000.exceptions.Error as exc:
                if number == len(attempts):
                    raise DatabaseUnavailable(_explain_connect_failure(exc, settings)) from exc
        try:
            # Guardrails, not the security boundary: the role's privileges are.
            conn.run("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            conn.run(f"SET statement_timeout = {int(settings.statement_timeout_ms)}")
        except pg8000.exceptions.Error as exc:
            conn.close()
            raise DatabaseUnavailable(f"Connected, but could not set up a read-only session: {exc}") from exc
        return conn


def _run_one_statement(conn: pg8000.native.Connection, sql: str, params: Dict[str, Any]) -> List[Sequence[Any]]:
    # Always the extended query protocol, where PostgreSQL rejects a string with
    # several statements. pg8000's run() without parameters uses the simple
    # protocol instead, which executes "SELECT 1; COMMIT; DROP ..." in full.
    statement = conn.prepare(sql)
    try:
        return statement.run(**params)
    finally:
        statement.close()


def pgpass_path(env: Mapping[str, str]) -> str:
    if env.get("PGPASSFILE"):
        return env["PGPASSFILE"]
    if sys.platform == "win32":
        return os.path.join(env.get("APPDATA") or os.path.expanduser("~"), "postgresql", "pgpass.conf")
    return os.path.join(os.path.expanduser("~"), ".pgpass")


def read_pgpass(path: str, host: str, port: int, database: str, user: str) -> Optional[str]:
    """Look the password up the way libpq does: the first matching line wins, * matches anything."""
    try:
        if sys.platform != "win32" and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None  # like libpq, ignore a password file that others can read
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
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


def _split_pgpass_line(line: str) -> List[str]:
    # Fields are separated by ":"; "\:" and "\\" stand for a literal colon and backslash.
    fields: List[str] = []
    current: List[str] = []
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


def _ssl_attempts(settings: Settings) -> List[Any]:
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


def _error_fields(exc: Exception) -> Dict[str, str]:
    # pg8000 passes the server's error message fields (C = SQLSTATE, M = text, H = hint) as a dict.
    return exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}


def _server_message(exc: Exception) -> str:
    fields = _error_fields(exc)
    if not fields:
        return str(exc)
    text = f"{fields.get('M', 'error')} (SQLSTATE {fields.get('C', '?')})"
    if fields.get("H"):
        text += f". Hint: {fields['H']}"
    return text


def _explain_connect_failure(exc: Exception, settings: Settings) -> str:
    cause = exc.__cause__
    fields = _error_fields(exc)
    code = fields.get("C")
    detail = fields.get("M") or str(exc)
    where = f"{settings.host}:{settings.port}"
    if isinstance(cause, (ConnectionRefusedError, FileNotFoundError)):
        return (
            f"Nothing accepts connections at {where}. Check PGHOST and PGPORT, "
            "and that the database is reachable from this machine (VPN?)."
        )
    if isinstance(cause, socket.gaierror):
        return f"Host {settings.host!r} cannot be resolved. Check PGHOST."
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return f"Connecting to {where} timed out. The host may be behind a firewall or a VPN."
    if code == "28P01" or (not settings.password and "password" in detail.lower()):
        return (
            f"Login as {settings.user!r} failed: {detail}. Put the password into {settings.passfile} "
            "as one line host:port:database:user:password (file mode 600). Never paste it into the chat."
        )
    if code == "3D000":
        return f"Database {settings.database!r} does not exist at {where}. Check PGDATABASE."
    if code == "28000":
        return f"The server refused the login for {settings.user!r}: {detail}. Check the role and pg_hba.conf with the DBA."
    if isinstance(cause, ssl.SSLError) or "SSL" in detail:
        return f"TLS with {where} failed: {cause or detail}. Check PGSSLMODE and PGSSLROOTCERT."
    return f"Cannot connect to {settings.describe()}: {detail}"


def _int_setting(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
