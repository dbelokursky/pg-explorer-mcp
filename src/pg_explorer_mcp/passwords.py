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
import contextlib
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

        PasswordStoreError says why nothing was written. Nothing to add writes nothing, and creates nothing.
        """
        if not passwords:
            return
        with self._lock:
            lines = merged(self._store.read().splitlines(), passwords)
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


def merged(lines: Sequence[str], passwords: Mapping[Login, str]) -> list[str]:
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


# --- the tools of the stores ----------------------------------------------------------------------------

# What a store was doing when its tool failed: the keys of the messages that name the operation.
READING = "passwords.reading"
WRITING = "passwords.writing"
REMOVING = "passwords.removing"


def _silent(kind: str, failure: OSError | subprocess.TimeoutExpired, operation: str) -> str:
    """Why the tool of a store could not do its work, quoting nothing of its exception.

    operation names what the store was doing (READING, WRITING, REMOVING). The text of a TimeoutExpired
    quotes the whole command, and on macOS the command of a writing carries the record as an argument.
    Told are the operation and the cause alone.
    """
    if isinstance(failure, subprocess.TimeoutExpired):
        return t(f"passwords.{kind}_timeout", operation=t(operation), seconds=TOOL_TIMEOUT_S)
    return t(
        f"passwords.{kind}_unstarted",
        operation=t(operation),
        error=failure.strerror or type(failure).__name__,
    )


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
        code, _, err = _security(
            "add-generic-password", "-U", "-s", self._service, "-a", self._account, "-w", record, operation=WRITING
        )
        if code != 0:
            raise PasswordStoreError(t("passwords.keychain_error", error=err.strip()))

    def forget(self) -> None:
        """Remove the record: the tests leave the keychain as they found it."""
        _security("delete-generic-password", "-s", self._service, "-a", self._account, operation=REMOVING)


TOOL_TIMEOUT_S = 30  # the tool answers in milliseconds; anything longer is a dialog waiting for the user


def _command(*args: str) -> list[str]:
    """The keychain tool with its arguments, as the tests may replace with a script of their own."""
    return [SECURITY, *args]


def _security(*args: str, operation: str = READING) -> tuple[int, str, str]:
    """Run the keychain tool as the agent runs the server: no terminal, and no endless waiting.

    A failure is told without the exception of the tool (_silent): the command of a writing carries the record,
    and the exception quotes the command. Raised outside the except clause, so that the tool's exception
    is chained to none of ours.
    """
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
        failure = exc
    else:
        return done.returncode, done.stdout, done.stderr
    raise PasswordStoreError(_silent("keychain", failure, operation))


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


# --- Linux: the keyring of the desktop ----------------------------------------------------------------

SECRET_TOOL = "secret-tool"  # noqa: S105  (the name of a tool, not a secret)
PART_BYTES = 6000  # of UTF-8 in a part: its base64 stays under the 8192 bytes secret-tool reads from its input
KEYRING_LABEL = "pg-explorer-mcp passwords"  # how the items are named in the tool of the desktop
Pair = tuple[str, str]  # an attribute of an item and its value, which are not secret and go as arguments


def _keyring_command(tool: str, *args: str) -> list[str]:
    """The keyring tool with its arguments, as the tests may replace with a script of their own."""
    return [tool, *args]


def _secret_tool(tool: str, *args: str, secret: str = "", operation: str = READING) -> tuple[int, str, str]:
    """Run the keyring tool as the agent runs the server: the secret on its input, a limited time to answer.

    A failure is told without the exception of the tool (_silent), which quotes its command.
    """
    try:
        done = subprocess.run(  # noqa: S603  (the system's own tool, by its full path, with arguments of ours)
            _keyring_command(tool, *args),
            input=secret,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure = exc
    else:
        return done.returncode, done.stdout, done.stderr
    raise PasswordStoreError(_silent("keyring", failure, operation))


def _pieces(text: str) -> list[str]:
    """The record in pieces of at most PART_BYTES of UTF-8, each cut where a character ends."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if current and size + width > PART_BYTES:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    pieces.append("".join(current))  # an empty record is one empty piece: the head must always find a part
    return pieces


class SecretService:
    """The keyring of the Linux desktop, through the secret-tool utility of libsecret (Secret Service).

    GNOME Keyring, KWallet and KeePassXC keep such a keyring, encrypted with a key unlocked at login.
    The utility reads a secret of 8 KiB at most, and one user may have hundreds of logins: the record
    lies there as the parts of a generation, and the head, an item of its own, names the generation.
    The parts go in first and the head last, so whatever interrupts a writing, the head names parts
    that are all there. A secret goes in on the standard input and comes out on the standard output,
    never as an argument.
    """

    def __init__(
        self,
        tool: str = SECRET_TOOL,
        service: str = SERVICE,
        account: str = ACCOUNT,
        collection: str | None = None,
    ) -> None:
        """The record under this service and account, written by this tool.

        collection, when given, is the object path of a collection of the keyring to write into, the
        default one otherwise: the tests of a machine without a login keyring name the session collection.
        """
        self._tool = tool
        self._service = service
        self._account = account
        self._collection = collection

    @property
    def label(self) -> str:
        """What status calls the store, in the server's language."""
        return t("passwords.secret_service", service=self._service, account=self._account)

    def read(self) -> str:
        """The record as the server wrote it; "" when nothing is stored yet."""
        head = ""
        for _ in range(3):
            head = self._item(("kind", "head")) or ""
            if not head:
                return ""
            if not head.isdigit():
                raise PasswordStoreError(self._not_ours(ValueError(head)))
            parts = self._parts(head)
            if parts:
                return "".join(parts)
            # A record always has a part. Without one, the record was rewritten between its head and its
            # parts: the old generation is gone and the head is about to name the new one. Once more.
        raise PasswordStoreError(
            t("passwords.keyring_missing", generation=head, service=self._service, account=self._account)
        )

    def _parts(self, generation: str) -> list[str]:
        """The parts of a generation in order, as long as there is a part: an absent one ends the record."""
        parts: list[str] = []
        number = 0
        while (part := self._item(("generation", generation), ("part", str(number)))) is not None:
            parts.append(part)
            number += 1
        return parts

    def write(self, text: str) -> None:
        """Replace the record: the parts of a new generation first, then the head that names them."""
        previous = self._item(("kind", "head"), operation=WRITING)
        if previous is not None and not previous.isdigit():
            raise PasswordStoreError(self._not_ours(ValueError(previous)))
        generation = str(int(previous or "0") + 1)
        for number, piece in enumerate(_pieces(text)):
            self._store(piece, ("generation", generation), ("part", str(number)))
        self._store(generation, ("kind", "head"))
        if previous is not None:
            with contextlib.suppress(PasswordStoreError):
                self._clear(("generation", previous))  # the best effort: a leftover is sealed and unused

    def forget(self) -> None:
        """Remove every item of the record: the tests leave the keyring as they found it."""
        self._clear(operation=REMOVING)

    def _item(self, *attributes: Pair, operation: str = READING) -> str | None:
        """The secret of the item with these attributes; None when there is none."""
        code, out, err = _secret_tool(self._tool, "lookup", *self._arguments(*attributes), operation=operation)
        if code == 1 and not err.strip():
            return None  # the utility's own answer to "no such item": it says nothing
        if code != 0:
            raise PasswordStoreError(self._failed(err))
        try:
            return base64.b64decode(out.strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError) as exc:
            raise PasswordStoreError(self._not_ours(exc)) from exc

    def _store(self, secret: str, *attributes: Pair, operation: str = WRITING) -> None:
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        code, _, err = _secret_tool(
            self._tool,
            "store",
            f"--label={KEYRING_LABEL}",
            *([f"--collection={self._collection}"] if self._collection else []),
            *self._arguments(*attributes),
            secret=encoded,
            operation=operation,
        )
        if code != 0:
            raise PasswordStoreError(self._failed(err))

    def _clear(self, *attributes: Pair, operation: str = WRITING) -> None:
        """Remove the items with these attributes: nothing to remove is the utility's exit code 1, as is an error."""
        code, _, err = _secret_tool(self._tool, "clear", *self._arguments(*attributes), operation=operation)
        if code != 0 and err.strip():
            raise PasswordStoreError(self._failed(err))

    def _arguments(self, *attributes: Pair) -> list[str]:
        arguments = ["service", self._service, "account", self._account]
        for name, value in attributes:
            arguments += [name, value]
        return arguments

    def _failed(self, error: str) -> str:
        return t("passwords.keyring_error", error=error.strip())

    def _not_ours(self, error: OSError | ValueError) -> str:
        return t("passwords.keyring_not_ours", error=error, service=self._service, account=self._account)


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
    tool = shutil.which(SECRET_TOOL)
    if sys.platform.startswith("linux") and tool:
        return SecretService(tool)
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
