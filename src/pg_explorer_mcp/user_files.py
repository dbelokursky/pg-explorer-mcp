"""Putting a new text in place of a user's file: the stands registry, the password file, a tool's settings.

The new text goes next to the file first, in a waiting file of its own, so a folder that cannot be
written changes nothing, and two commands writing one file do not write into one waiting file. The
old file is kept as a dated copy, and the new one takes its place in one rename: a reader finds the
file there at every moment, and when the new one cannot be put in place the old one never moved.
The files may hold passwords (a .pgpass, a pg_service.conf), so a new text is readable by its owner
only while it waits. What it is afterwards is the caller's choice: the permissions of the old file,
or the owner's alone, for the files install.py keeps for itself.

What a command reads, merges and writes is one cycle: two commands making it at once over one file
would each write from what they read before the other wrote, and one change would be lost. So the
commands of install.py hold, over it, the lock of exclusive(): one at a time, over processes.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# The path as the caller names it, the file it is (a link resolved) and the new text next to that file.
Staged = tuple[Path, Path, Path]
NEW = ".pg-explorer-mcp-new-"  # the waiting text, <name>.pg-explorer-mcp-new-<of its own>.tmp, next to the file
_OWNER_ONLY = 0o600

LOCK = "lock"  # the file a command holds while it reads, merges and writes the user's files
LOCK_TIMEOUT_S = 10  # how long the next command waits for the one before it


class StillLockedError(Exception):
    """Another command held the lock for the whole time allowed; nothing of the block ran."""


def stage(path: Path, text: str | bytes) -> Staged:
    """Write the new text, or bytes, next to the file the path is: readable by its owner only, flushed to disk.

    The waiting file is this text's alone: a second text for the same file waits in another.
    """
    real = path.resolve()  # a link (into a dotfiles repository, say) stays a link to the changed file
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor, waiting = tempfile.mkstemp(prefix=f"{real.name}{NEW}", suffix=".tmp", dir=real.parent)
    except OSError as exc:
        raise OSError(exc.errno, exc.strerror, real) from exc
    temporary = Path(waiting)
    try:
        if isinstance(text, bytes):
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            # Text goes as text, with the line ends of the system, as it always went.
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        discard([(path, real, temporary)])
        raise
    return path, real, temporary


def place(staged: Staged, *, owner_only: bool = False) -> Path | None:
    """Put a staged text in place of its file; the old one is kept as a dated copy.

    The dated copy, or None when the file is new. The new text takes the file's place in one rename:
    a reader finds the file there at every moment. When it cannot be put in place, the dated copy
    goes and the error is raised: the file is as it was. The new file takes the old one's permissions,
    or with owner_only the owner's alone, for a file that may hold passwords; its dated copy is then
    no more open than that either.
    """
    _, real, temporary = staged
    if not real.exists():
        temporary.replace(real)
        return None
    backup = backup_name(real)
    if owner_only:
        if sys.platform != "win32" and stat.S_IMODE(real.stat().st_mode) & 0o077:
            real.chmod(_OWNER_ONLY)  # while nothing has been written: nothing changed if this fails
    else:
        shutil.copymode(real, temporary)
    shutil.copy2(real, backup)
    try:
        temporary.replace(real)
    except OSError:
        with contextlib.suppress(OSError):
            backup.unlink()  # nothing was replaced: there is no old file to keep a copy of
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


def unplace(staged: Staged, backup: Path | None) -> None:
    """Take a placed text back, when a later step failed: the dated copy is the file again, or the new file goes.

    What cannot be undone is left as it is: the dated copy is the user's way back.
    """
    _, real, _ = staged
    with contextlib.suppress(OSError):
        if backup is None:
            real.unlink()
        else:
            backup.replace(real)


def discard(staged: Sequence[Staged]) -> None:
    """Remove the new texts that wait next to their files, the ones not placed yet."""
    for _, _, temporary in staged:
        with contextlib.suppress(OSError):  # not there, or not even its folder
            temporary.unlink()


# --- one command at a time ----------------------------------------------------------------------------


@contextlib.contextmanager
def exclusive(folder: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold, over processes, the lock of this folder while the block reads, merges and writes the user's files.

    The next command waits for the one before it, no longer than timeout seconds; StillLockedError says it
    did not get in, and nothing of the block ran. Where no lock can be made the work goes on without
    one: the files are still replaced whole, only a change made at that moment by another command
    can then be lost. A command that dies lets its lock go with it: the system closes the file.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(folder / LOCK, os.O_RDWR | os.O_CREAT, _OWNER_ONLY)
    except OSError:
        yield
        return
    try:
        _wait_for(descriptor, timeout)
        yield
    finally:
        os.close(descriptor)


def _wait_for(descriptor: int, timeout: float) -> None:
    """Take the lock, trying again until the time allowed is over."""
    due = time.monotonic() + timeout
    while (failure := _taking(descriptor)) is not None:
        if not _held(failure):
            raise failure  # not another command: the lock file itself cannot be used
        if time.monotonic() >= due:
            raise StillLockedError(f"another command holds {LOCK!r}") from failure
        time.sleep(0.05)


def _taking(descriptor: int) -> OSError | None:
    """Take the lock; the error when it was not taken, for whichever reason."""
    try:
        _lock(descriptor)
    except OSError as exc:
        return exc
    return None


def _lock(descriptor: int) -> None:
    if sys.platform == "win32":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _held(error: OSError) -> bool:
    """Whether the error says that another command holds the lock."""
    if sys.platform == "win32":
        return error.errno == errno.EACCES  # what _locking answers to a region another command holds
    return isinstance(error, BlockingIOError)
