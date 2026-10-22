"""Static check of the SQL the agent may run: read-only statements only.

This is the third line of defence from ADR 0002. The role's privileges and a
READ ONLY transaction that is rolled back after every call stop writes even if
this check misses something. Its job is to refuse early, with a reason the
model can act on, and to catch side effects a READ ONLY transaction does not stop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .i18n import t

ALLOWED_FIRST_WORDS = ("select", "with", "values", "table", "explain", "show")
ROW_SOURCES = ("select", "with", "values", "table")  # statements that can feed a cursor

# Words that only occur in statements or clauses that change data; the text sql.word.<word> says why.
FORBIDDEN_WORDS = ("insert", "update", "delete", "merge", "into")

# Function -> reason, the text sql.reason.<reason>.
FORBIDDEN_FUNCTIONS: dict[str, str] = {
    "pg_terminate_backend": "ends_session",
    "pg_cancel_backend": "cancels_query",
    "set_config": "settings",
    "query_to_xml": "unseen_sql",
    "query_to_xmlschema": "unseen_sql",
    "query_to_xml_and_xmlschema": "unseen_sql",
    "cursor_to_xml": "unseen_cursor",
    "cursor_to_xmlschema": "unseen_cursor",
    "pg_read_file": "reads_files",
    "pg_read_binary_file": "reads_files",
    "pg_stat_file": "inspects_files",
    "lo_import": "reads_files",
    "lo_export": "writes_files",
    "lo_create": "large_objects",
    "lo_creat": "large_objects",
    "lo_unlink": "large_objects",
    "lo_put": "large_objects",
    "lo_from_bytea": "large_objects",
    "lowrite": "large_objects",
    "lo_truncate": "large_objects",
    "lo_truncate64": "large_objects",
    "pg_reload_conf": "administers",
    "pg_rotate_logfile": "administers",
    "pg_switch_wal": "administers",
    "pg_create_restore_point": "administers",
    "pg_promote": "administers",
    "pg_backup_start": "administers",
    "pg_backup_stop": "administers",
    "pg_log_backend_memory_contexts": "signals",
    "pg_logical_emit_message": "wal",
    "pg_import_system_collations": "catalog",
    "pg_notify": "wakes",
    "nextval": "sequence",
    "setval": "sequence",
    "pg_sleep": "sleeps",
    "pg_sleep_for": "sleeps",
    "pg_sleep_until": "sleeps",
}

FORBIDDEN_PREFIXES: tuple[tuple[str, str], ...] = (
    ("dblink", "dblink"),
    ("pg_ls_", "lists_files"),
    ("pg_stat_reset", "resets_stats"),
    ("pg_wal_replay_", "replication"),
    ("pg_file_", "writes_files"),
)

# Checked only for pg_* names: advisory locks outlive the query, slots steer replication.
FORBIDDEN_FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("advisory", "advisory"),
    ("replication_slot", "slots"),
)

_WORD = re.compile(r"[^\W\d][\w$]*")
_NUMBER = re.compile(r"\d[\w.]*")
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")


class SqlRejectedError(Exception):
    """The statement is not allowed; the message says why."""


@dataclass(frozen=True)
class Token:
    kind: str  # "word" (lower-cased), "quoted" (identifier), "literal", "number" or "punct"
    text: str


def check_read_only(sql: object) -> str:
    """Return the statement's first keyword, or raise SqlRejectedError with the reason."""
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejectedError(t("sql.empty"))
    tokens = tokenize(sql)
    if tokens and tokens[-1] == Token("punct", ";"):
        tokens = tokens[:-1]
    if not tokens:
        raise SqlRejectedError(t("sql.only_comments"))
    if Token("punct", ";") in tokens:
        raise SqlRejectedError(t("sql.one_statement"))

    first = tokens[0]
    if first.kind != "word" or first.text not in ALLOWED_FIRST_WORDS:
        if first.kind == "word":
            raise SqlRejectedError(t("sql.not_allowed", word=first.text.upper(), allowed=t("sql.summary")))
        raise SqlRejectedError(t("sql.not_allowed_other", allowed=t("sql.summary")))
    if first.text == "explain":
        explained = _explained_statement(tokens)
        if explained not in ROW_SOURCES:
            raise SqlRejectedError(t("sql.explain"))

    for index, token in enumerate(tokens):
        if token.kind != "word":
            continue
        if token.text in FORBIDDEN_WORDS:
            reason = t(f"sql.word.{token.text}")
            raise SqlRejectedError(t("sql.forbidden_word", word=token.text.upper(), reason=reason))
        if token.text == "for" and _word_after(tokens, index) in ("share", "key"):
            raise SqlRejectedError(t("sql.row_locks"))
        if token.kind == "word" and _word_after(tokens, index, punct="(") is not None:
            _check_function(token.text)
    for index, token in enumerate(tokens[:-1]):
        if token.kind == "quoted" and tokens[index + 1] == Token("punct", "("):
            _check_function(token.text)
    return first.text


def forbidden_function(name: str) -> str | None:
    """Why the function may not be called, in the current language; None if it may."""
    lowered = name.lower()
    reason = FORBIDDEN_FUNCTIONS.get(lowered)
    if reason is None:
        reason = next((reason for prefix, reason in FORBIDDEN_PREFIXES if lowered.startswith(prefix)), None)
    if reason is None and lowered.startswith("pg_"):
        reason = next((reason for fragment, reason in FORBIDDEN_FRAGMENTS if fragment in lowered), None)
    return None if reason is None else t(f"sql.reason.{reason}")


def tokenize(sql: str) -> list[Token]:
    """Split SQL into tokens, skipping whitespace, comments and the inside of literals."""
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        if char.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            i = _skip_block_comment(sql, i)
        elif (
            char == "'"
            or (char in "eEbBxXnN" and sql.startswith("'", i + 1))
            or (char in "uU" and sql.startswith("&'", i + 1))
        ):
            quote = sql.index("'", i)
            i = _skip_quoted(sql, quote, "'", backslashes=char in "eE")
            tokens.append(Token("literal", ""))
        elif char == '"' or (char in "uU" and sql.startswith('&"', i + 1)):
            quote = sql.index('"', i)
            end = _skip_quoted(sql, quote, '"', backslashes=False)
            tokens.append(Token("quoted", sql[quote + 1 : end - 1].replace('""', '"')))
            i = end
        elif char == "$" and _DOLLAR_TAG.match(sql, i):
            tag = _DOLLAR_TAG.match(sql, i).group(0)  # type: ignore[union-attr]
            end = sql.find(tag, i + len(tag))
            if end < 0:
                raise SqlRejectedError(t("sql.unterminated_dollar"))
            i = end + len(tag)
            tokens.append(Token("literal", ""))
        elif _WORD.match(sql, i):
            match = _WORD.match(sql, i)
            tokens.append(Token("word", match.group(0).lower()))  # type: ignore[union-attr]
            i = match.end()  # type: ignore[union-attr]
        elif char.isdigit():
            i = _NUMBER.match(sql, i).end()  # type: ignore[union-attr]
            tokens.append(Token("number", ""))
        else:
            tokens.append(Token("punct", char))
            i += 1
    return tokens


def _check_function(name: str) -> None:
    reason = forbidden_function(name)
    if reason:
        raise SqlRejectedError(t("sql.function", name=name, reason=reason))


def _word_after(tokens: list[Token], index: int, punct: str | None = None) -> str | None:
    """The next token's text if it is a word (or, with punct, that punctuation)."""
    if index + 1 >= len(tokens):
        return None
    following = tokens[index + 1]
    if punct is not None:
        return following.text if following == Token("punct", punct) else None
    return following.text if following.kind == "word" else None


def _explained_statement(tokens: list[Token]) -> str | None:
    """First keyword of the statement after EXPLAIN and its options."""
    position = 1
    if position < len(tokens) and tokens[position] == Token("punct", "("):
        depth = 0
        while position < len(tokens):
            if tokens[position] == Token("punct", "("):
                depth += 1
            elif tokens[position] == Token("punct", ")"):
                depth -= 1
                if depth == 0:
                    position += 1
                    break
            position += 1
    while position < len(tokens) and tokens[position].text in ("analyze", "analyse", "verbose"):
        position += 1
    if position < len(tokens) and tokens[position].kind == "word":
        return tokens[position].text
    return None


def _skip_block_comment(sql: str, start: int) -> int:
    depth, i = 0, start
    while i < len(sql):
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise SqlRejectedError(t("sql.unterminated_comment"))


def _skip_quoted(sql: str, start: int, quote: str, *, backslashes: bool) -> int:
    """Index just past the closing quote; a doubled quote stands for itself."""
    i = start + 1
    while i < len(sql):
        if backslashes and sql[i] == "\\":
            i += 2
        elif sql[i] == quote:
            if sql.startswith(quote, i + 1):
                i += 2
            else:
                return i + 1
        else:
            i += 1
    raise SqlRejectedError(t("sql.unterminated_quote", quote=quote))
