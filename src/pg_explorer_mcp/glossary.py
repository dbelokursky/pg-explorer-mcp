"""Business descriptions kept beside the database, in a file of the user's folder.

Databases written before the agents often have no COMMENT ON anything: the glossary
file is where a group writes, in its own languages, what its tables and columns mean
and what its business terms are. The tools show a glossary text where the database
holds no comment of its own, and search_schema also matches the texts, so a question
in the group's words finds the tables behind them (ADR 0010).

The file uses the pg_service.conf format, a section per thing described:

    [table shop.orders]
    ru = Заказы клиентов: статус и время
    [column shop.orders.status]
    en = new, paid, shipped or cancelled
    [term average_receipt]
    ru = Сумма заказов со статусом paid за месяц

Only en and ru texts are read, in that spirit: whichever the server speaks comes
first, the other stands in when it is missing. The file is re-read when it changes
on disk, the way the stands registry is, so an edit needs no restart.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import current, t
from .stands import parse_service_file

LANGUAGES = ("en", "ru")  # the keys of a section, and the order a missing language falls back to
GLOSSARY_SEARCH_LIMIT = 5  # matches a search adds from the glossary, after the catalog's own


@dataclass(frozen=True)
class Entry:
    """One thing the glossary describes, with a text per language that has one."""

    kind: str  # table, column or term
    name: str  # shop.orders, shop.orders.status, average_receipt
    texts: Mapping[str, str]

    def text(self) -> str:
        """The text in the language the server speaks, or in the other one when it is missing."""
        for language in (current(), *(lang for lang in LANGUAGES if lang != current())):
            if self.texts.get(language):
                return self.texts[language]
        return ""


@dataclass
class Glossary:
    """The entries of the glossary file, re-read when the file changes on disk."""

    entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    state: dict[str, tuple[int, int] | None] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str], files: list[Path] | None = None) -> Glossary:
        """The glossary the environment names; an unreadable file is a warning, never a failure."""
        paths = (
            [Path(env["PG_EXPLORER_MCP_GLOSSARY_FILE"]).expanduser()]
            if env.get("PG_EXPLORER_MCP_GLOSSARY_FILE")
            else files
            if files is not None
            else [default_file(env)]
        )
        return cls._read(paths)

    @classmethod
    def _read(cls, paths: list[Path]) -> Glossary:
        glossary = cls(paths=paths, state={str(path): _stamp(path) for path in paths})
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue  # no glossary is the usual case: nothing to describe
            except (OSError, UnicodeDecodeError) as exc:
                glossary.warnings.append(t("glossary.unreadable", path=path, error=exc))
                continue
            sections, problems = parse_service_file(text)
            glossary.warnings.extend(problems)
            for name, options in sections.items():
                entry = _entry(name, options)
                if entry is not None:
                    glossary.entries.append(entry)
        return glossary

    def refresh(self) -> bool:
        """Re-read the files when one changed on disk; True when the entries are new ones."""
        if {str(path): _stamp(path) for path in self.paths} == self.state:
            return False
        fresh = Glossary._read(self.paths)
        self.entries, self.warnings, self.state = fresh.entries, fresh.warnings, fresh.state
        return True

    def describe(self, kind: str, name: str) -> str | None:
        """The text for a thing of that kind and name, or None when the glossary holds none."""
        wanted = name.lower()
        for entry in self.entries:
            if entry.kind == kind and entry.name.lower() == wanted and entry.text():
                return entry.text()
        return None

    def search(self, fragment: str, limit: int = GLOSSARY_SEARCH_LIMIT) -> list[Entry]:
        """The entries whose text in either language holds the fragment, tables and columns first.

        A match by text is what the glossary is for: the group's words for a thing lead to it
        even when its name says nothing.
        """
        needle = fragment.casefold()
        # A term's name spells its underscore as a space in a question: "average receipt" asks for average_receipt.
        found = [
            entry
            for entry in self.entries
            if needle in entry.name.casefold().replace("_", " ") or needle in entry.text().casefold()
        ]
        named = [entry for entry in found if entry.kind != "term"]
        terms = [entry for entry in found if entry.kind == "term"]
        return [*named, *terms][:limit]


def default_file(env: Mapping[str, str]) -> Path:
    """glossary.conf in the folder of pg-explorer-mcp, beside the stands registry."""
    home = Path(env["APPDATA"]) if sys.platform == "win32" and env.get("APPDATA") else Path.home()
    hidden = "" if sys.platform == "win32" else "."
    return home / f"{hidden}pg-explorer-mcp" / "glossary.conf"


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_mtime_ns, found.st_size


def _entry(name: str, options: Mapping[str, str]) -> Entry | None:
    """The entry a section describes: "[table shop.orders]" with en=/ru= lines."""
    kind, _, rest = name.strip().partition(" ")
    if kind not in ("table", "column", "term") or not rest.strip():
        return None  # an ordinary service section, or a line the parser already complained about
    texts = {language: options[language].strip() for language in LANGUAGES if options.get(language)}
    if not texts:
        return None  # a section without a text in a language the server speaks describes nothing
    return Entry(kind, rest.strip(), texts)
