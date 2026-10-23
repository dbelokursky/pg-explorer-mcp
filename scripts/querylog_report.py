#!/usr/bin/env python3
"""Sum up the query log for the administrator of a group: who asked what, and what was refused.

    python3 scripts/querylog_report.py ~/pg-mcp-queries.jsonl
    python3 scripts/querylog_report.py queries.jsonl queries.jsonl.1 queries.jsonl.2.gz --since 2026-10-20
    python3 scripts/querylog_report.py queries.jsonl --format csv --by user > users.csv

The log is what PG_MCP_QUERY_LOG collects (README, "Query log"): one JSON line per tool call. Name
every file to read, rotated ones (.1, .gz) included; gzip is told by content, not by name. --since
and --until take a UTC date or date-time; a bare date in --until covers that whole day. A line that
is not a JSON object with time, tool, outcome and duration_ms, say one cut by a crash or by rotation,
is skipped and counted. The text report is for a terminal: the period, the calls by tool, by user and
by stand, the median and 95th percentile of duration_ms, the ten longest calls and every call the SQL
check refused, since those are the attempts to write. --format json gives the same figures to scripts
and --format csv one table, the breakdown that --by chooses. Arguments, the SQL among them, go on one
line cut to 80 characters, and the value of a key that looks like a password is hidden.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import functools
import gzip
import heapq
import io
import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OUTCOMES = ("ok", "error", "refused", "unavailable", "failed", "skipped")  # skipped: a stand of a call over several
BREAKDOWNS = ("tool", "user", "stand", "day")
FORMATS = ("text", "json", "csv")
TOP = 10  # longest calls shown
ARGUMENTS_WIDTH = 80
UNKNOWN_USER = "(unknown)"  # os_user is null when the server found no login name
# The server never logs a password, but the value of a key by one of these names is hidden anyway.
SECRET_KEYS = ("password", "passwd", "pwd", "secret", "token")
GZIP_MAGIC = b"\x1f\x8b"
UTC = dt.timezone.utc

# The columns of each breakdown: the same in the text table, the CSV and the JSON.
COLUMNS: dict[str, tuple[str, ...]] = {
    "tool": ("tool", "calls", "ok", "error", "refused", "unavailable", "failed", "problems_pct"),
    "user": ("user", "calls", "queries", "rows", "refused"),
    "stand": ("stand", "calls", "ok", "error", "refused", "unavailable", "failed", "skipped", "problems_pct"),
    "day": ("day", "calls", "users", "ok", "error", "refused", "unavailable", "failed", "problems_pct"),
}
TEXT_HEADERS = {"problems_pct": "problems"}


@dataclass(frozen=True)
class Entry:
    """One tool call: as much of its line as the report needs."""

    time: dt.datetime  # UTC
    user: str
    tool: str
    outcome: str
    arguments: str  # one line, cut to ARGUMENTS_WIDTH, secrets hidden
    stand: str | None  # a call on one stand
    stands: dict[str, str]  # a call over several stands: the outcome on each
    rows: int | None
    duration_ms: float
    message: str | None

    @property
    def day(self) -> str:
        return self.time.date().isoformat()

    @property
    def where(self) -> str:
        """The stand of the call for the detail lists; a call over several stands names their number."""
        if self.stands:
            return f"{len(self.stands)} stands"
        return self.stand or ""


def parse_entry(line: str) -> Entry | None:
    """The call a log line describes, or None when the line is not a complete line of the query log."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    time, duration = parse_time(raw.get("time")), _number(raw.get("duration_ms"))
    tool, outcome = raw.get("tool"), raw.get("outcome")
    if time is None or duration is None or not isinstance(tool, str) or not isinstance(outcome, str):
        return None
    user, stand, stands, rows, message = (raw.get(key) for key in ("os_user", "stand", "stands", "rows", "message"))
    return Entry(
        time=time,
        user=user if isinstance(user, str) and user else UNKNOWN_USER,
        tool=tool,
        outcome=outcome,
        arguments=render_arguments(raw.get("arguments")),
        stand=stand if isinstance(stand, str) else None,
        stands={str(name): str(result) for name, result in stands.items()} if isinstance(stands, dict) else {},
        rows=int(rows) if isinstance(rows, (int, float)) and not isinstance(rows, bool) else None,
        duration_ms=duration,
        message=message if isinstance(message, str) else None,
    )


def _number(value: object) -> float | None:
    """The value as a number; None for anything else, True and False included."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_time(value: object) -> dt.datetime | None:
    """The UTC moment an ISO 8601 text names (the log writes 2026-10-22T13:54:20.570Z); None for anything else."""
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def parse_moment(text: str, *, end_of_day: bool = False) -> dt.datetime:
    """A --since or --until value: a UTC date or date-time.

    A bare date is its first moment, or with end_of_day the midnight after it, so that --until covers the day.
    """
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        moment = parse_time(text)
        if moment is None:
            raise argparse.ArgumentTypeError(f"not a date or date-time: {text}") from None
        return moment
    start = dt.datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start + dt.timedelta(days=1) if end_of_day else start


def render_arguments(arguments: object, width: int = ARGUMENTS_WIDTH) -> str:
    """The arguments as `key=value` pairs on one line, cut to the width; the value of a secret-looking key hidden."""
    if isinstance(arguments, dict):
        text = " ".join(f"{key}={_plain(_hide(key, value))}" for key, value in arguments.items())
    else:
        text = _plain(arguments)
    return text if len(text) <= width else text[: width - 1] + "…"


def _hide(key: object, value: object) -> object:
    if isinstance(key, str) and any(word in key.lower() for word in SECRET_KEYS):
        return "***"
    if isinstance(value, dict):
        return {name: _hide(name, item) for name, item in value.items()}
    return value


def _plain(value: object) -> str:
    """A value on one line: text as it is, anything else as JSON; runs of whitespace, newlines among them, folded."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())


def read_lines(path: Path) -> Iterator[str]:
    """The lines of a plain or gzip-compressed file; gzip is told by its first two bytes, whatever the name."""
    with path.open("rb") as raw:
        compressed = raw.read(len(GZIP_MAGIC)) == GZIP_MAGIC
    if compressed:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as zipped:
            yield from zipped
    else:
        with path.open(encoding="utf-8", errors="replace") as plain:
            yield from plain


def percentile(values: Sequence[float], share: float) -> float:
    """The nearest-rank percentile: the smallest value that at least `share` of the values do not exceed."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(share * len(ordered))) - 1]


@dataclass
class Bucket:
    """The calls of one tool, user, stand or day."""

    calls: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    queries: int = 0  # calls whose own statement ran: the lines with `rows`
    rows: int = 0
    users: set[str] = field(default_factory=set)

    def add(self, outcome: str, entry: Entry | None = None) -> None:
        self.calls += 1
        self.outcomes[outcome] += 1
        if entry is not None:
            self.users.add(entry.user)
            if entry.rows is not None:
                self.queries += 1
                self.rows += entry.rows

    @property
    def problems_pct(self) -> float:
        """The share of calls that did not end well; a stand skipped for want of time is no fault of the stand."""
        problems = self.calls - self.outcomes["ok"] - self.outcomes["skipped"]
        return round(100 * problems / self.calls, 1) if self.calls else 0.0

    def row(self, kind: str, name: str) -> dict[str, object]:
        values: dict[str, object] = {
            kind: name,
            "calls": self.calls,
            "users": len(self.users),
            "queries": self.queries,
            "rows": self.rows,
            "problems_pct": self.problems_pct,
            **{outcome: self.outcomes[outcome] for outcome in OUTCOMES},
        }
        return {column: values[column] for column in COLUMNS[kind]}


class Report:
    """What the log says about one period, gathered line by line so that a long log needs little memory."""

    def __init__(self, since: dt.datetime | None = None, until: dt.datetime | None = None) -> None:
        self.since = since
        self.until = until
        self.files: list[str] = []
        self.lines = 0  # lines read, blank ones aside
        self.broken = 0  # of them, not complete log lines
        self.calls = 0  # calls within the period
        self.first: dt.datetime | None = None
        self.last: dt.datetime | None = None
        self.outcomes: Counter[str] = Counter()
        self.buckets: dict[str, dict[str, Bucket]] = {kind: {} for kind in BREAKDOWNS}
        self.durations: list[float] = []
        self._refused: list[Entry] = []
        self._longest: list[tuple[float, int, Entry]] = []  # a heap of the TOP longest calls

    def read(self, path: Path) -> None:
        """Add the calls of one file; a line that is not a complete log line counts as broken."""
        self.files.append(str(path))
        for line in read_lines(path):
            if not line.strip():
                continue
            self.lines += 1
            entry = parse_entry(line)
            if entry is None:
                self.broken += 1
            elif self.covers(entry.time):
                self.add(entry)

    def covers(self, moment: dt.datetime) -> bool:
        return (self.since is None or moment >= self.since) and (self.until is None or moment < self.until)

    def add(self, entry: Entry) -> None:
        self.calls += 1
        self.first = entry.time if self.first is None else min(self.first, entry.time)
        self.last = entry.time if self.last is None else max(self.last, entry.time)
        self.outcomes[entry.outcome] += 1
        self.durations.append(entry.duration_ms)
        item = (entry.duration_ms, self.calls, entry)  # the running number keeps equal durations apart
        if len(self._longest) < TOP:
            heapq.heappush(self._longest, item)
        else:
            heapq.heappushpop(self._longest, item)
        if entry.outcome == "refused":
            self._refused.append(entry)
        for kind, name in (("tool", entry.tool), ("user", entry.user), ("day", entry.day)):
            self.bucket(kind, name).add(entry.outcome, entry)
        if entry.stand is not None:
            self.bucket("stand", entry.stand).add(entry.outcome, entry)
        for stand, outcome in entry.stands.items():
            # The rows of a call over several stands are one total: they belong to no single stand.
            self.bucket("stand", stand).add(outcome)

    def bucket(self, kind: str, name: str) -> Bucket:
        return self.buckets[kind].setdefault(name, Bucket())

    @property
    def stands_mode(self) -> bool:
        return bool(self.buckets["stand"])

    @property
    def longest(self) -> list[Entry]:
        return [entry for _, _, entry in sorted(self._longest, key=lambda item: (-item[0], item[1]))]

    @property
    def refused(self) -> list[Entry]:
        """The calls the SQL check refused, in order of time whatever the order of the files."""
        return sorted(self._refused, key=lambda entry: entry.time)

    @property
    def median(self) -> float | None:
        return statistics.median(self.durations) if self.durations else None

    @property
    def p95(self) -> float | None:
        return percentile(self.durations, 0.95) if self.durations else None

    def table(self, kind: str) -> list[dict[str, object]]:
        """The rows of one breakdown: days in order, the rest by calls, most first."""
        buckets = self.buckets[kind]
        names = sorted(buckets) if kind == "day" else sorted(buckets, key=lambda name: (-buckets[name].calls, name))
        return [buckets[name].row(kind, name) for name in names]

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "lines": self.lines,
            "broken": self.broken,
            "since": self.since,
            "until": self.until,
            "first": self.first,
            "last": self.last,
            "calls": self.calls,
            "outcomes": {outcome: self.outcomes[outcome] for outcome in OUTCOMES if outcome != "skipped"},
            "duration_ms": {"median": self.median, "p95": self.p95},
            "tools": self.table("tool"),
            "users": self.table("user"),
            "stands": self.table("stand"),
            "days": self.table("day"),
            "longest": [detail(entry) for entry in self.longest],
            "refused": [detail(entry) for entry in self.refused],
        }


def detail(entry: Entry) -> dict[str, object]:
    """One call of the detail lists, the same fields in the text and in the JSON."""
    return {
        "time": entry.time,
        "user": entry.user,
        "stand": entry.where or None,
        "tool": entry.tool,
        "arguments": entry.arguments,
        "duration_ms": entry.duration_ms,
        "message": entry.message,
    }


def stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cell_text(column: str, value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return stamp(value)
    if isinstance(value, float):
        return f"{value:.1f}%" if column == "problems_pct" else f"{value:.1f}"
    return str(value)


def table_text(columns: Sequence[str], rows: Sequence[dict[str, object]], indent: str = "  ") -> list[str]:
    """A table for the terminal: a header line, numbers aligned right, text left, two spaces between columns."""
    headers = [TEXT_HEADERS.get(column, column) for column in columns]
    grid = [[cell_text(column, row[column]) for column in columns] for row in rows]
    numeric = [bool(rows) and _number(rows[0][column]) is not None for column in columns]
    widths = [max([len(header), *(len(cells[i]) for cells in grid)]) for i, header in enumerate(headers)]

    def line(cells: Sequence[str]) -> str:
        padded = (cell.rjust(w) if right else cell.ljust(w) for cell, w, right in zip(cells, widths, numeric))
        return (indent + "  ".join(padded)).rstrip()

    return [line(headers), *(line(cells) for cells in grid)]


def detail_columns(report: Report, *, duration: bool) -> list[str]:
    columns = ["time", "user", "stand", "tool", "arguments"]
    if not report.stands_mode:
        columns.remove("stand")
    return ["duration_ms", *columns] if duration else columns


def render_text(report: Report, by: str) -> str:
    lines = [f"Files: {', '.join(report.files)} ({report.lines} lines, {report.broken} broken)"]
    if report.since or report.until:
        edges = (("from", report.since), ("before", report.until))
        lines.append("Filter: " + ", ".join(f"{word} {stamp(moment)}" for word, moment in edges if moment) + " UTC")
    counted = "".join(f"  {outcome} {report.outcomes[outcome]}" for outcome in OUTCOMES if outcome != "skipped")
    lines.append(f"Calls: {report.calls}{counted}")
    first, last, median, p95 = report.first, report.last, report.median, report.p95
    if first is None or last is None or median is None or p95 is None:  # no calls in the period
        return "\n".join(lines) + "\n"
    lines.append(f"Period: {stamp(first)} .. {stamp(last)} UTC")
    counts = f"Users: {len(report.buckets['user'])}  Tools: {len(report.buckets['tool'])}"
    lines.append(counts + (f"  Stands: {len(report.buckets['stand'])}" if report.stands_mode else ""))
    lines.append(f"Duration: median {median:.1f} ms, 95th percentile {p95:.1f} ms")
    kinds = ["tool", "user", *(["stand"] if report.stands_mode else []), *(["day"] if by == "day" else [])]
    for kind in kinds:
        lines.extend(["", f"By {kind}", *table_text(COLUMNS[kind], report.table(kind))])
    longest = [detail(entry) for entry in report.longest]
    lines.extend(["", f"Longest calls (top {len(longest)})"])
    lines.extend(table_text(detail_columns(report, duration=True), longest))
    refused = [detail(entry) for entry in report.refused]
    lines.extend(["", f"Refused by the SQL check ({len(refused)})"])
    lines.extend(table_text(detail_columns(report, duration=False), refused) if refused else ["  none"])
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=_json_default) + "\n"


def _json_default(value: object) -> str:
    return iso(value) if isinstance(value, dt.datetime) else str(value)


def render_csv(report: Report, by: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS[by])
    writer.writerows([row[column] for column in COLUMNS[by]] for row in report.table(by))
    return buffer.getvalue()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sum up the query log: calls by tool, user and stand, the longest calls and the refused SQL."
    )
    parser.add_argument("files", nargs="+", type=Path, metavar="FILE", help="query log files, rotated (.1, .gz) too")
    parser.add_argument("--since", type=parse_moment, metavar="WHEN", help="count calls from this UTC date or time on")
    parser.add_argument(
        "--until",
        type=functools.partial(parse_moment, end_of_day=True),
        metavar="WHEN",
        help="count calls before this UTC date-time; a bare date counts its whole day",
    )
    parser.add_argument(
        "--by",
        choices=BREAKDOWNS,
        default="tool",
        help="the breakdown that --format csv prints (default: tool); in the text report, day adds a table by day",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default="text",
        help="text for a terminal (default), json with every figure, csv with the one breakdown of --by",
    )
    args = parser.parse_args(argv)
    if args.since and args.until and args.since >= args.until:
        parser.error("--since must be before --until")

    report = Report(args.since, args.until)
    try:
        for path in args.files:
            report.read(path)
    except (OSError, EOFError) as exc:  # EOFError: a gzip file cut short
        parser.error(f"cannot read {report.files[-1]}: {exc}")

    if hasattr(sys.stdout, "reconfigure"):
        # UTF-8 wherever the report goes: a file or pipe on Windows would get the console code page otherwise.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.format == "json":
        text = render_json(report)
    elif args.format == "csv":
        text = render_csv(report, args.by)
    else:
        text = render_text(report, args.by)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
