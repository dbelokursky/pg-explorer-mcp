"""Queries the group wrote, kept beside the database: the agent runs them by name.

A group that does not want the agent writing its own SQL still wants it to answer
questions with data: the DBA writes the query once, with the limits and the joins the
way the group counts them, and the agent calls it by name (ADR 0011). Each query is
checked by the read-only guard when the file is read, so a query that changes anything
is a warning, not a tool that runs it.

The file uses the pg_service.conf format, a section per query, beside the stands
registry and the glossary:

    [orders_by_status]
    ru = Заказы по статусам
    en = Orders by status
    query = SELECT status, count(*) FROM shop.orders GROUP BY status ORDER BY 2 DESC

The tool appears only when the file holds a query, so a group that does not use it
carries none of its tokens. An edited file is re-read when it changes on disk, like
the stands registry; parameters are a later question (ADR 0011).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import current, t
from .sqlguard import ROW_SOURCES, SqlRejectedError, check_read_only
from .stands import _stamp, parse_service_file

LANGUAGES = ("en", "ru")  # the keys of a section, and the order a missing language falls back to
_NAME = re.compile(r"[A-Za-z0-9_$-]+")


@dataclass(frozen=True)
class SavedQuery:
    """One query the group wrote, with its descriptions and its read-only SQL."""

    name: str
    texts: Mapping[str, str]
    sql: str

    def text(self) -> str:
        """The description in the language the server speaks, or in the other one when it is missing."""
        for language in (current(), *(lang for lang in LANGUAGES if lang != current())):
            if self.texts.get(language):
                return self.texts[language]
        return ""


@dataclass
class SavedQueries:
    """The queries of the file, re-read when it changes on disk."""

    queries: list[SavedQuery] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    state: dict[str, tuple[int, int] | None] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str], files: list[Path] | None = None) -> SavedQueries:
        """The queries the environment names; an unreadable file is a warning, never a failure."""
        paths = (
            [Path(env["PG_EXPLORER_MCP_QUERIES_FILE"]).expanduser()]
            if env.get("PG_EXPLORER_MCP_QUERIES_FILE")
            else files
            if files is not None
            else [default_file(env)]
        )
        return cls._read(paths)

    @classmethod
    def _read(cls, paths: list[Path]) -> SavedQueries:
        saved = cls(paths=paths, state={str(path): _stamp(path) for path in paths})
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue  # no saved queries is the usual case: the tool stays away entirely
            except (OSError, UnicodeDecodeError) as exc:
                saved.warnings.append(t("queries.unreadable", path=path, error=exc))
                continue
            sections, problems = parse_service_file(text)
            saved.warnings.extend(problems)
            for name, options in sections.items():
                try:
                    query = _query(name, options)
                except _RefusedError as refused:
                    saved.warnings.append(str(refused))
                    continue
                if query is not None:
                    saved.queries.append(query)
        return saved

    def refresh(self) -> bool:
        """Re-read the files when one changed on disk; True when the queries are new ones."""
        if {str(path): _stamp(path) for path in self.paths} == self.state:
            return False
        fresh = SavedQueries._read(self.paths)
        self.queries, self.warnings, self.state = fresh.queries, fresh.warnings, fresh.state
        return True

    def names(self) -> list[str]:
        return [query.name for query in self.queries]

    def named(self, name: str) -> SavedQuery | None:
        wanted = name.strip().lower()
        for query in self.queries:
            if query.name.lower() == wanted:
                return query
        return None


def default_file(env: Mapping[str, str]) -> Path:
    """queries.conf in the folder of pg-explorer-mcp, beside the stands registry and the glossary."""
    home = Path(env["APPDATA"]) if sys.platform == "win32" and env.get("APPDATA") else Path.home()
    hidden = "" if sys.platform == "win32" else "."
    return home / f"{hidden}pg-explorer-mcp" / "queries.conf"


def _query(name: str, options: Mapping[str, str]) -> SavedQuery | None:
    """The query a section describes; what the guard turns down raises _RefusedError for a warning."""
    sql = options.get("query", "").strip()
    if not _NAME.fullmatch(name.strip()) or not sql:
        return None  # an ordinary service section, or a section without a query: not ours
    texts = {language: options[language].strip() for language in LANGUAGES if options.get(language)}
    try:
        if check_read_only(sql) not in ROW_SOURCES:
            raise _RefusedError(t("queries.not_rows", name=name.strip()))
        return SavedQuery(name.strip(), texts, sql)
    except SqlRejectedError as exc:
        raise _RefusedError(t("queries.refused", name=name.strip(), message=exc)) from exc


class _RefusedError(Exception):
    """A saved query the read-only guard turned down; the caller turns it into a warning."""
