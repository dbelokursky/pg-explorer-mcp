"""Static check of the SQL the agent may run: read-only statements only.

This is the third line of defence from ADR 0002. The role's privileges and a
READ ONLY transaction that is rolled back after every call stop writes even if
this check misses something. Its job is to refuse early, with a reason the
model can act on, and to catch side effects a READ ONLY transaction does not stop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_FIRST_WORDS = ("select", "with", "values", "table", "explain", "show")
ROW_SOURCES = ("select", "with", "values", "table")  # statements that can feed a cursor

# Words that only occur in statements or clauses that change data.
FORBIDDEN_WORDS: dict[str, str] = {
    "insert": "INSERT changes data",
    "update": "UPDATE changes data, and FOR UPDATE locks rows",
    "delete": "DELETE changes data",
    "merge": "MERGE changes data",
    "into": "SELECT ... INTO creates a table",
}

FORBIDDEN_FUNCTIONS: dict[str, str] = {
    "pg_terminate_backend": "it ends another session",
    "pg_cancel_backend": "it cancels another session's query",
    "set_config": "it changes session settings",
    "query_to_xml": "it runs SQL text this check never saw",
    "query_to_xmlschema": "it runs SQL text this check never saw",
    "query_to_xml_and_xmlschema": "it runs SQL text this check never saw",
    "cursor_to_xml": "it reads a cursor this check never saw",
    "cursor_to_xmlschema": "it reads a cursor this check never saw",
    "pg_read_file": "it reads files on the database server",
    "pg_read_binary_file": "it reads files on the database server",
    "pg_stat_file": "it inspects files on the database server",
    "lo_import": "it reads files on the database server",
    "lo_export": "it writes files on the database server",
    "lo_create": "it changes large objects",
    "lo_creat": "it changes large objects",
    "lo_unlink": "it changes large objects",
    "lo_put": "it changes large objects",
    "lo_from_bytea": "it changes large objects",
    "lowrite": "it changes large objects",
    "lo_truncate": "it changes large objects",
    "lo_truncate64": "it changes large objects",
    "pg_reload_conf": "it administers the server",
    "pg_rotate_logfile": "it administers the server",
    "pg_switch_wal": "it administers the server",
    "pg_create_restore_point": "it administers the server",
    "pg_promote": "it administers the server",
    "pg_backup_start": "it administers the server",
    "pg_backup_stop": "it administers the server",
    "pg_log_backend_memory_contexts": "it signals another session",
    "pg_logical_emit_message": "it writes to the WAL",
    "pg_import_system_collations": "it changes the catalog",
    "pg_notify": "it wakes other sessions",
    "nextval": "it changes a sequence",
    "setval": "it changes a sequence",
    "pg_sleep": "it holds the connection doing nothing",
    "pg_sleep_for": "it holds the connection doing nothing",
    "pg_sleep_until": "it holds the connection doing nothing",
}

FORBIDDEN_PREFIXES: tuple[tuple[str, str], ...] = (
    ("dblink", "it opens a separate connection that this server's read-only mode does not cover"),
    ("pg_ls_", "it lists files on the database server"),
    ("pg_stat_reset", "it resets statistics"),
    ("pg_wal_replay_", "it controls replication"),
    ("pg_file_", "it writes files on the database server"),
)

# Checked only for pg_* names: advisory locks outlive the query, slots steer replication.
FORBIDDEN_FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("advisory", "advisory locks outlive the query and block other sessions"),
    ("replication_slot", "it changes replication slots"),
)

ALLOWED_SUMMARY = "SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN and SHOW"

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
        raise SqlRejectedError("query must be a non-empty string with one SQL statement")
    tokens = tokenize(sql)
    if tokens and tokens[-1] == Token("punct", ";"):
        tokens = tokens[:-1]
    if not tokens:
        raise SqlRejectedError("The query has no statement, only comments.")
    if Token("punct", ";") in tokens:
        raise SqlRejectedError("Send one statement per call: the query has ';' between statements.")

    first = tokens[0]
    if first.kind != "word" or first.text not in ALLOWED_FIRST_WORDS:
        shown = first.text.upper() if first.kind == "word" else "This statement"
        raise SqlRejectedError(f"{shown} is not allowed. Only read-only statements are: {ALLOWED_SUMMARY}.")
    if first.text == "explain":
        explained = _explained_statement(tokens)
        if explained not in ROW_SOURCES:
            raise SqlRejectedError("EXPLAIN is allowed only for SELECT, WITH ... SELECT, VALUES and TABLE.")

    for index, token in enumerate(tokens):
        if token.kind != "word":
            continue
        if token.text in FORBIDDEN_WORDS:
            raise SqlRejectedError(
                f"{token.text.upper()} is not allowed: {FORBIDDEN_WORDS[token.text]}. This server only reads."
            )
        if token.text == "for" and _word_after(tokens, index) in ("share", "key"):
            raise SqlRejectedError("Row locks (FOR SHARE, FOR KEY SHARE) are not allowed: they block other sessions.")
        if token.kind == "word" and _word_after(tokens, index, punct="(") is not None:
            _check_function(token.text)
    for index, token in enumerate(tokens[:-1]):
        if token.kind == "quoted" and tokens[index + 1] == Token("punct", "("):
            _check_function(token.text)
    return first.text


def forbidden_function(name: str) -> str | None:
    lowered = name.lower()
    if lowered in FORBIDDEN_FUNCTIONS:
        return FORBIDDEN_FUNCTIONS[lowered]
    for prefix, reason in FORBIDDEN_PREFIXES:
        if lowered.startswith(prefix):
            return reason
    if lowered.startswith("pg_"):
        for fragment, reason in FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                return reason
    return None


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
                raise SqlRejectedError("Unterminated dollar-quoted string.")
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
        raise SqlRejectedError(f"Function {name}() is not allowed: {reason}.")


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
    raise SqlRejectedError("Unterminated /* comment.")


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
    raise SqlRejectedError(f"Unterminated {quote} quote.")
