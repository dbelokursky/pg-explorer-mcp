"""Putting a new text in place of a user's file: the stands registry, the password file, a tool's settings.

The new text goes next to the file first, so a folder that cannot be written changes nothing. The
old file becomes a dated copy, and when the new one cannot be put in place the old one goes back:
however this fails, the user has the file they had or the one meant to replace it. The files may
hold passwords (a .pgpass, a pg_service.conf), so a new text is readable by its owner only while it
waits. What it is afterwards is the caller's choice: the permissions of the old file, or the owner's
alone, for the files install.py keeps for itself.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import shutil
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

# The path as the caller names it, the file it is (a link resolved) and the new text next to that file.
Staged = tuple[Path, Path, Path]
NEW = ".pg-explorer-mcp-new"  # the suffix of a new text while it waits next to its file
_OWNER_ONLY = 0o600


def stage(path: Path, text: str) -> Staged:
    """Write the new text next to the file the path is, readable by its owner only and flushed to disk."""
    real = path.resolve()  # a link (into a dotfiles repository, say) stays a link to the changed file
    temporary = real.with_name(real.name + NEW)
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if sys.platform != "win32":
            temporary.chmod(_OWNER_ONLY)  # whatever the umask made of the mode given to open
    except OSError:
        discard([(path, real, temporary)])
        raise
    return path, real, temporary


def place(staged: Staged, *, owner_only: bool = False) -> Path | None:
    """Put a staged text in place of its file; the old one becomes a dated copy.

    The dated copy, or None when the file is new. When the new text cannot be put in place, the old
    file goes back where it was and the error is raised. The new file takes the old one's permissions,
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
            real.chmod(_OWNER_ONLY)  # while it is still the file itself: nothing has moved if this fails
    else:
        shutil.copymode(real, temporary)
    real.replace(backup)
    try:
        temporary.replace(real)
    except OSError:
        backup.replace(real)  # the old file goes back where it was
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


def discard(staged: Sequence[Staged]) -> None:
    """Remove the new texts that wait next to their files, the ones not placed yet."""
    for _, _, temporary in staged:
        with contextlib.suppress(OSError):  # not there, or not even its folder
            temporary.unlink()
