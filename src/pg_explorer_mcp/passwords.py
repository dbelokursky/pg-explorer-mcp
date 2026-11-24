"""The server's passwords: one record in the secure storage of the system (ADR 0012).

Inside the record the lines are those of the libpq password file, host:port:database:user:password,
and they match the way libpq matches them: the first line that fits wins, * stands for anything.
The system keeps the record encrypted with a key of its own: the login keychain of macOS, DPAPI
on Windows, the keyring of a Linux desktop. Nothing of ours is written in clear, and the server
has no cryptography of its own.

A password is taken from the first place that has one: PGPASSWORD in the environment, the store,
the libpq password file (for those who had one before the store; status warns about it), and the
password= line of a registry section (warned about too). The server never writes the store:
`install.py password` and `install.py import` do.
"""

from __future__ import annotations

import base64
import binascii
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from . import dpapi
from .i18n import t
from .user_files import place, stage

SERVICE = "pg-explorer-mcp"  # how the system's storage names the record
ACCOUNT = "passwords"
# Switched off by PG_EXPLORER_MCP_PASSWORD_STORE=off: this machine's store is neither read nor written.
STORE_VARIABLE = "PG_EXPLORER_MCP_PASSWORD_STORE"
# What a registry section's password= becomes for the login of that section (stands.py).
REGISTRY_PASSWORD = "PG_EXPLORER_MCP_REGISTRY_PASSWORD"  # noqa: S105  (the name of a variable, not a secret)
REFRESH_S = 300  # the record is read again when this much has passed since the last reading
OFF = ("off", "no", "false", "0")
PASSWORD_COMMAND = "python3 install.py password"  # noqa: S105  (how a password gets into the store; said in hints)

# Where a password came from.
ENVIRONMENT = "environment"
STORE = "store"
FILE = "file"
REGISTRY = "registry"
NAMES = {ENVIRONMENT: "passwords.the_environment", STORE: "passwords.the_store", REGISTRY: "passwords.the_registry"}

Login = tuple[str, str, str, str]  # host, port, database, user: what a line is found by


class PasswordStoreError(Exception):
    """The password store cannot be read or written; the message says what to do and holds no password."""


class Store(Protocol):
    """The secure storage of a system as the server uses it: one text record, read and written whole."""

    @property
    def label(self) -> str:
        """What status calls the store, in the server's language."""
        ...

    def read(self) -> str:
        """The record as it was written, or "" when nothing is stored yet; PasswordStoreError when it cannot be."""
        ...

    def write(self, text: str) -> None:
        """Replace the record with this text, whole; PasswordStoreError when it was not done."""
        ...


# --- lines of the password file -------------------------------------------------------------------


def split_line(line: str) -> list[str]:
    r"""The fields of a line: separated by ":", where "\:" and "\\" stand for a literal colon and backslash."""
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


def line_of(login: Login, password: str) -> str:
    """The line of a login's password, with a colon or a backslash inside a field escaped."""
    return ":".join(_escape(field) for field in (*login, password))


def _escape(field: str) -> str:
    return field.replace("\\", "\\\\").replace(":", "\\:")


def login_of(line: str) -> Login | None:
    """The login a line is written for, when the line is a complete one; None for a comment or a broken line."""
    fields = split_line(line)
    return (fields[0], fields[1], fields[2], fields[3]) if len(fields) == 5 else None


def matches(lines: Sequence[str], host: str, port: int, database: str, user: str) -> str | None:
    """The password of the first line that fits, as libpq picks it; None when no line does."""
    # libpq matches Unix-socket connections against "localhost".
    wanted = ("localhost" if host.startswith("/") else host, str(port), database, user)
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = split_line(line)
        if len(fields) == 5 and all(field in ("*", value) for field, value in zip(fields, wanted)):
            return fields[4]
    return None


def pgpass_path(env: Mapping[str, str]) -> str:
    """The libpq password file: PGPASSFILE's, or the psql default of the platform."""
    if env.get("PGPASSFILE"):
        return env["PGPASSFILE"]
    if sys.platform == "win32":
        return str(Path(env.get("APPDATA") or Path.home()) / "postgresql" / "pgpass.conf")
    return str(Path.home() / ".pgpass")


def read_pgpass(path: str, host: str, port: int, database: str, user: str) -> str | None:
    """Look a password up in the libpq password file the way libpq does."""
    passfile = Path(path)
    try:
        if sys.platform != "win32" and passfile.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None  # like libpq, ignore a password file that others can read
        lines = passfile.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return matches(lines, host, port, database, user)


# --- where a password is taken from ----------------------------------------------------------------


def lookup(env: Mapping[str, str], host: str, port: int, database: str, user: str) -> tuple[str | None, str | None]:
    """The password of a login and where it came from (ADR 0012); (None, None) when nothing has it."""
    given = env.get("PGPASSWORD")
    if given:
        return given, ENVIRONMENT
    store = current(env)
    if store is not None:
        found = store.lookup(host, port, database, user)
        if found is not None:
            return found, STORE
    found = read_pgpass(pgpass_path(env), host, port, database, user)
    if found is not None:
        return found, FILE
    given = env.get(REGISTRY_PASSWORD)
    return (given, REGISTRY) if given else (None, None)


# --- the store -------------------------------------------------------------------------------------


class Passwords:
    """The store's lines, read at most once in REFRESH_S seconds: a call over many stands pays once."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._lines: list[str] | None = None
        self._read_at = 0.0
        self._problem: str | None = None

    @property
    def label(self) -> str:
        """What status calls the store of this system."""
        return self._store.label

    def lookup(self, host: str, port: int, database: str, user: str) -> str | None:
        """The password the store holds for the login; None when it holds none, or cannot be read (see problem)."""
        return matches(self._fresh(), host, port, database, user)

    def problem(self) -> str | None:
        """Why the store could not be read the last time it was tried, or None when it could."""
        with self._lock:
            return self._problem

    def put(self, passwords: Mapping[Login, str]) -> None:
        """Add these passwords, a line per login, in place of the lines of the same logins; written at once.

        PasswordStoreError says why nothing was written.
        """
        with self._lock:
            lines = _merged(self._store.read().splitlines(), passwords)
            self._store.write("\n".join(lines) + ("\n" if lines else ""))
            self._lines, self._read_at, self._problem = lines, time.monotonic(), None

    def _fresh(self) -> list[str]:
        """The lines, reading the store when none were read yet or the last reading has grown old."""
        with self._lock:
            if self._lines is None or time.monotonic() - self._read_at >= REFRESH_S:
                try:
                    self._lines = self._store.read().splitlines()
                    self._problem = None
                except PasswordStoreError as exc:
                    self._lines, self._problem = [], str(exc)
                self._read_at = time.monotonic()
            return self._lines


def _merged(lines: Sequence[str], passwords: Mapping[Login, str]) -> list[str]:
    """The lines with these passwords in place of those of the same logins, and a line for each new login."""
    pending = dict(passwords)
    out = []
    for line in lines:
        login = login_of(line)
        if login is not None and login in pending:
            out.append(line_of(login, pending.pop(login)))
        else:
            out.append(line)
    out.extend(line_of(login, password) for login, password in pending.items())
    return out


def without(lines: Sequence[str], logins: Sequence[Login]) -> list[str]:
    """The lines that are not written for exactly one of these logins; a line with * in it serves other logins."""
    return [line for line in lines if login_of(line) not in set(logins)]


# --- macOS: the login keychain ---------------------------------------------------------------------


SECURITY = "/usr/bin/security"


class Keychain:
    """The login keychain of macOS: one generic password, read and written with the security tool.

    The tool is trusted for the items it created, so a server the agent starts, without a terminal,
    reads the record without a dialog.
    """

    def __init__(self, service: str = SERVICE, account: str = ACCOUNT) -> None:
        """The record under this service and account; the tests use names of their own and delete what they made."""
        self._service = service
        self._account = account

    @property
    def label(self) -> str:
        """What status calls the store, in the server's language."""
        return t("passwords.keychain")

    def read(self) -> str:
        """The record as the server wrote it; "" when there is none yet."""
        code, out, err = _security("find-generic-password", "-s", self._service, "-a", self._account, "-w")
        if code == 44:  # the tool's own answer to "no such item": nothing is stored yet
            return ""
        if code != 0:
            raise PasswordStoreError(t("passwords.keychain_error", error=err.strip()))
        try:
            return base64.b64decode(out.strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError) as exc:
            raise PasswordStoreError(
                t("passwords.not_ours", error=exc, service=self._service, account=self._account)
            ) from exc

    def write(self, text: str) -> None:
        """Replace the record, or create it; the tool takes the text only as an argument, so base64 on one line."""
        record = base64.b64encode(text.encode("utf-8")).decode("ascii")
        code, _, err = _security("add-generic-password", "-U", "-s", self._service, "-a", self._account, "-w", record)
        if code != 0:
            raise PasswordStoreError(t("passwords.keychain_error", error=err.strip()))

    def forget(self) -> None:
        """Remove the record: the tests leave the keychain as they found it."""
        _security("delete-generic-password", "-s", self._service, "-a", self._account)


TOOL_TIMEOUT_S = 30  # the tool answers in milliseconds; anything longer is a dialog waiting for the user


def _command(*args: str) -> list[str]:
    """The keychain tool with its arguments, as the tests may replace with a script of their own."""
    return [SECURITY, *args]


def _security(*args: str) -> tuple[int, str, str]:
    """Run the keychain tool as the agent runs the server: no terminal, and no endless waiting."""
    try:
        done = subprocess.run(  # noqa: S603  (the system's own tool, by its full path, with arguments of ours)
            _command(*args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PasswordStoreError(t("passwords.keychain_silent", error=exc)) from exc
    return done.returncode, done.stdout, done.stderr


# --- Windows: a file sealed by DPAPI -----------------------------------------------------------------


def windows_file(env: Mapping[str, str]) -> Path:
    r"""Where the sealed passwords are on Windows: %APPDATA%\pg-explorer-mcp\passwords."""
    return Path(env.get("APPDATA") or Path.home()) / SERVICE / ACCOUNT


class DpapiFile:
    """The passwords of the server in one file on Windows, sealed by DPAPI for the account.

    DPAPI encrypts with a key Windows keeps for the account that asked: another account of that
    machine cannot open the file, nor can the same account on another machine. The file is replaced
    whole, with a dated copy of the old one (user_files.py); nothing is asked of the user.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def label(self) -> str:
        """What status calls the store, in the server's language."""
        return t("passwords.dpapi", path=self._path)

    def read(self) -> str:
        """The lines as the server wrote them; "" when the file does not exist yet."""
        try:
            sealed = self._path.read_bytes()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise PasswordStoreError(self._failed(exc)) from exc
        try:
            return dpapi.unseal(sealed).decode("utf-8")
        except (OSError, ValueError) as exc:
            raise PasswordStoreError(self._failed(exc)) from exc

    def write(self, text: str) -> None:
        """Replace the file with these lines, sealed."""
        try:
            place(stage(self._path, dpapi.seal(text.encode("utf-8"))))
        except OSError as exc:
            raise PasswordStoreError(self._failed(exc)) from exc

    def _failed(self, error: OSError | ValueError) -> str:
        return t("passwords.dpapi_error", path=self._path, error=error)


# --- which store this process uses ------------------------------------------------------------------


def is_off(env: Mapping[str, str]) -> bool:
    """Whether PG_EXPLORER_MCP_PASSWORD_STORE switches the store off."""
    return env.get(STORE_VARIABLE, "").strip().lower() in OFF


def system(env: Mapping[str, str]) -> Store | None:
    """The store to use on this machine, or None: switched off, or nothing here can keep a secret."""
    return None if is_off(env) else _of_this_system(env)


def _of_this_system(env: Mapping[str, str]) -> Store | None:
    if sys.platform == "darwin" and shutil.which(SECURITY):
        return Keychain()
    if sys.platform == "win32":
        return DpapiFile(windows_file(env))
    return None


class _Process:
    """What this process decided about the store: once, whatever thread asks first."""

    lock = threading.Lock()
    passwords: Passwords | None = None
    decided = False


_process = _Process()


def current(env: Mapping[str, str]) -> Passwords | None:
    """The password store of this process, or None when there is none to use."""
    with _process.lock:
        if not _process.decided:
            store = system(env)
            _process.passwords = Passwords(store) if store is not None else None
            _process.decided = True
    return _process.passwords


def use(store: Store | None) -> None:
    """The store of this process from now on, whatever the system has: the tests give one of their own."""
    with _process.lock:
        _process.passwords = Passwords(store) if store is not None else None
        _process.decided = True
