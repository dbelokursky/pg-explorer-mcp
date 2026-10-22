"""The query log: one JSON line per tool call, in a file the user chooses.

Off unless PG_MCP_QUERY_LOG names a file. A line says who asked what and how it
ended: time, OS user, process, role and database, the tool and its arguments (the
SQL text among them), the outcome with the refusal or error text, how many rows the
agent's own statement returned, and how long the call took. Rows themselves are
never written.

The file lives on the user's machine: it is a journal of what the agent did, not a
tamper-proof audit, which belongs on the database server (ADR 0005). Writing it never
breaks a tool call; status reports a file that cannot be written.
"""

from __future__ import annotations

import datetime
import getpass
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Database
from .i18n import t

log = logging.getLogger(__name__)

# How a tool call ended.
OK = "ok"
REFUSED = "refused"  # the read-only check turned the SQL down
ERROR = "error"  # bad arguments, or PostgreSQL rejected the statement
UNAVAILABLE = "unavailable"  # the database could not be reached
FAILED = "failed"  # a bug in the server; its stderr log has the traceback

# One write per line, always at the end of the file: several servers can share it, and
# opening it anew for every line lets logrotate move it away without a restart. A symlink in
# place of the file is refused, so a link planted in a shared folder cannot redirect the lines.
_OPEN_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
_FILE_MODE = 0o600  # SQL literals may carry personal data


@dataclass(frozen=True)
class Mark:
    """Where a tool call started: the clock and the database's counters."""

    started: float
    statements: int
    rows: int


class QueryLog:
    def __init__(self, path: str | None, db: Database | None = None) -> None:
        self.path = path
        self.problem: str | None = None
        self._db = db
        self._target = path if path and Path(path).is_absolute() else None
        if path and self._target is None:
            self.problem = t("querylog.relative")
        self._os_user = _os_user()

    @classmethod
    def from_env(cls, env: Mapping[str, str], db: Database | None = None) -> QueryLog:
        raw = env.get("PG_MCP_QUERY_LOG", "").strip()
        return cls(str(Path(raw).expanduser()) if raw else None, db)

    def describe(self) -> str:
        """The status line about the log."""
        if self.path is None:
            return t("status.query_log_off")
        if self.problem:
            return t("status.query_log_problem", path=self.path, problem=self.problem)
        return t("status.query_log", path=self.path)

    def start(self) -> Mark:
        db = self._db
        return Mark(time.monotonic(), db.statements_run if db else 0, db.rows_read if db else 0)

    def write(self, mark: Mark, tool: str, arguments: Any, outcome: str, message: str | None = None) -> None:
        if self._target is None:
            return
        db = self._db
        settings = db.settings if db else None
        entry: dict[str, Any] = {
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")[:-6] + "Z",
            "os_user": self._os_user,
            "pid": os.getpid(),
            "role": settings.user if settings else None,
            "database": settings.database if settings else None,
            "tool": tool,
            "arguments": arguments,
            "outcome": outcome,
        }
        if message:
            entry["message"] = message
        if db is not None and db.statements_run > mark.statements:
            entry["rows"] = db.rows_read - mark.rows
        entry["duration_ms"] = round((time.monotonic() - mark.started) * 1000, 1)
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        try:
            descriptor = os.open(self._target, _OPEN_FLAGS, _FILE_MODE)
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError as exc:
            if self.problem != str(exc):
                log.warning("cannot write the query log %s: %s", self._target, exc)
            self.problem = str(exc)
        else:
            self.problem = None


def _os_user() -> str | None:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no login name in the environment and no passwd entry either
        return None
