"""How much of a tool's answer reaches the model.

Local models run with small context windows, and a long answer can end a
GigaCode session outright ("API Error: terminated"). Every tool answer passes
through fit_to_budget, so no tool can flood the context, whatever it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from .i18n import t

log = logging.getLogger(__name__)

DEFAULT_BUDGET = 8000  # characters per tool answer
MIN_BUDGET = 500
VALUE_LIMIT = 200  # characters per comment, default or definition inside an answer


class Keep(str):
    """A line of an answer that must survive the cut: it says what the answer leaves out."""


def fit_to_budget(lines: list[str], budget: int) -> list[str]:
    """Trim a tool's answer to at most `budget` characters, newlines included.

    `lines` is the complete answer, one entry per line, the most important
    first: a header, then columns, then indexes. `budget` is at least MIN_BUDGET.

    The contract the tests check:
    - an answer that already fits comes back unchanged;
    - joined with newlines, the result is never longer than `budget`;
    - if anything was cut, the last line says so and starts with "…", and the
      lines before it are input lines in their order, the last one possibly shortened;
    - a line of the class `Keep` is among them whatever was cut, right before the
      notice, as long as it fits beside the notice.
    """
    if len("\n".join(lines)) <= budget:
        return lines
    total = len(lines)
    # Reserve room for the longest notice this answer can get, plus its newline, and for the lines that
    # must survive; those that cannot fit even so are cut like any other.
    tail = [line for line in lines if isinstance(line, Keep)]
    if len("\n".join(tail)) + 1 + len(_notice(budget, total, total)) > budget:
        tail = []
    head = [line for line in lines if not isinstance(line, Keep)] if tail else lines
    reserved = len(_notice(budget, total, total)) + 1 + len("\n".join(tail)) + (1 if tail else 0)
    kept: list[str] = []
    used = 0
    for line in head:
        cost = len(line) + (1 if kept else 0)
        if used + cost + reserved > budget:
            break
        kept.append(line)
        used += cost
    if not kept and budget - reserved > 1:
        # Even the first line is too long: keep its beginning rather than nothing.
        kept = [head[0][: budget - reserved - 1] + "…"]
    return [*kept, *tail, _notice(budget, len(kept) + len(tail), total)]


def _notice(budget: int, shown: int, total: int) -> str:
    return t("budget.notice", budget=budget, shown=shown, total=total)


def shorten(value: str, limit: int = VALUE_LIMIT) -> str:
    """One line of at most `limit` characters: whitespace collapsed, the tail replaced by "…"."""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def budget_from_env(env: Mapping[str, str]) -> int:
    """The response budget PG_EXPLORER_MCP_MAX_RESPONSE_CHARS names, with its fallbacks."""
    raw = env.get("PG_EXPLORER_MCP_MAX_RESPONSE_CHARS")
    if not raw:
        return DEFAULT_BUDGET
    try:
        value = int(raw)
    except ValueError:
        log.warning("PG_EXPLORER_MCP_MAX_RESPONSE_CHARS=%r is not a number, using %d", raw, DEFAULT_BUDGET)
        return DEFAULT_BUDGET
    return max(value, MIN_BUDGET)
