"""scripts/querylog_report.py: the summary of a query log, from lines the server writes and lines it does not."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import querylog_report

from tests.helpers import ROOT

UTC = dt.timezone.utc

# The two lines of the README, as the server wrote them.
README_LINES = """\
{"time": "2026-10-22T13:54:20.570Z", "os_user": "anna", "pid": 95715, "role": "mcp_demo_agent", "database": "pg_mcp_demo", "tool": "run_select", "arguments": {"query": "SELECT status, count(*) FROM shop.orders GROUP BY status"}, "outcome": "ok", "rows": 4, "duration_ms": 12.6}
{"time": "2026-10-22T13:54:20.571Z", "os_user": "anna", "pid": 95715, "role": "mcp_demo_agent", "database": "pg_mcp_demo", "tool": "run_select", "arguments": {"query": "DELETE FROM shop.orders WHERE status = 'cancelled'"}, "outcome": "refused", "message": "DELETE is not allowed. Only read-only statements are: SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN and SHOW.", "duration_ms": 0.0}
"""  # noqa: E501
DELETE_REFUSAL = (
    "DELETE is not allowed. Only read-only statements are: SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN and SHOW."
)
UPDATE_REFUSAL = DELETE_REFUSAL.replace("DELETE", "UPDATE")
LONG_SQL = (
    "SELECT i.id,\n       i.total\n  FROM billing.invoices i\n"
    "  JOIN billing.customers c ON c.id = i.customer_id\n WHERE c.country = 'DE'\n   AND i.total > 1000\n"
    " ORDER BY i.total DESC"
)


def call(time: str, tool: str, outcome: str, duration_ms: float, **rest: object) -> str:
    """A log line as the server writes it: a call by anna on one database unless the rest says otherwise."""
    entry: dict[str, object] = {"time": time, "os_user": "anna", "pid": 4242, "role": "mcp_agent", "database": "shop"}
    entry.update(rest)
    entry.update(tool=tool, arguments=entry.pop("arguments", {}), outcome=outcome, duration_ms=duration_ms)
    return json.dumps(entry, ensure_ascii=False)


# 2026-10-22: one database, three users (the third unknown), every outcome once at least.
DAY_1 = README_LINES + "\n".join(
    [
        call("2026-10-22T14:00:00.000Z", "describe_table", "ok", 30.0, arguments={"table": "shop.orders"}),
        call(
            "2026-10-22T14:05:00.000Z",
            "sample_rows",
            "ok",
            250.0,
            os_user="boris",
            arguments={"table": "shop.orders", "rows": 20},
            rows=20,
        ),
        call(
            "2026-10-22T14:10:00.000Z",
            "run_select",
            "error",
            5.0,
            os_user="boris",
            arguments={"query": "SELEC 1"},
            message='syntax error at or near "SELEC"',
        ),
        call("2026-10-22T15:00:00.000Z", "list_tables", "unavailable", 2000.0, os_user="boris", message="no route"),
        call(
            "2026-10-22T15:30:00.000Z", "status", "failed", 1.0, os_user=None, role=None, message="RuntimeError: boom"
        ),
        '{"time": "2026-10-22T15:31:00.000Z", "os_user": "anna", "pid": 4242, "tool": "status", "arg',  # cut short
        "not json at all",
        "",
    ]
)
# 2026-10-23: the stands mode, one call over several stands, a refused UPDATE with a password among the arguments.
DAY_2 = "\n".join(
    [
        call(
            "2026-10-23T09:00:00.000Z",
            "run_select",
            "ok",
            40.0,
            stand="dev7",
            schema="billing",
            arguments={"stand": "dev7", "query": "SELECT 1"},
            rows=10,
        ),
        call(
            "2026-10-23T09:05:00.000Z",
            "run_select",
            "ok",
            24990.0,
            role=None,
            database=None,
            stands={"dev1": "ok", "dev2": "unavailable", "dev3": "skipped"},
            arguments={"stands": "dev*", "query": "SELECT count(*) FROM billing.invoices"},
            rows=15,
        ),
        call(
            "2026-10-23T09:10:00.000Z",
            "run_select",
            "refused",
            0.1,
            os_user="boris",
            stand="dev7",
            schema="billing",
            arguments={"stand": "dev7", "query": "UPDATE billing.invoices SET paid = true", "password": "hunter2"},
            message=UPDATE_REFUSAL,
        ),
        call(
            "2026-10-23T10:00:00.000Z",
            "explain_query",
            "ok",
            100.0,
            stand="dev7",
            schema="billing",
            arguments={"stand": "dev7", "query": LONG_SQL},
            rows=3,
        ),
        "",
    ]
)
# 2026-10-24: one call and four lines that are not complete log lines.
DAY_3 = "\n".join(
    [
        call("2026-10-24T08:00:00.000Z", "run_select", "ok", 7.0, arguments={"query": "SELECT 1"}, rows=1),
        "[1, 2, 3]",
        '{"time": "2026-10-24T08:01:00Z", "outcome": "ok", "duration_ms": 1}',
        '{"time": "yesterday", "tool": "status", "outcome": "ok", "duration_ms": 1}',
        '{"time": "2026-10-24T08:02:00Z", "tool": "status", "outcome": "ok", "duration_ms": "fast"}',
        "",
        "",
    ]
)
CALLS = 12
BROKEN = 6


@pytest.fixture
def log_files(tmp_path: Path) -> list[str]:
    """The log as logrotate leaves it: the current file, yesterday's plain and the day before gzipped."""
    (tmp_path / "queries.jsonl").write_text(DAY_3, encoding="utf-8")
    (tmp_path / "queries.jsonl.1").write_text(DAY_2, encoding="utf-8")
    with gzip.open(tmp_path / "queries.jsonl.2.gz", "wt", encoding="utf-8") as zipped:
        zipped.write(DAY_1)
    return [str(tmp_path / name) for name in ("queries.jsonl", "queries.jsonl.1", "queries.jsonl.2.gz")]


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert querylog_report.main(list(argv)) == 0
    return capsys.readouterr().out


def section(text: str, title: str) -> list[list[str]]:
    """The rows of a table under its title, each split on whitespace; the header row first."""
    lines = text.splitlines()
    start = lines.index(title) + 1
    rows = []
    for entry in lines[start:]:
        if not entry.strip():
            break
        rows.append(entry.split())
    return rows


def test_complete_lines_are_counted_and_broken_ones_are_skipped(log_files: list[str]) -> None:
    report = querylog_report.Report()
    for path in log_files:
        report.read(Path(path))
    assert (report.lines, report.broken, report.calls) == (CALLS + BROKEN, BROKEN, CALLS)
    assert report.outcomes == {"ok": 7, "error": 1, "refused": 2, "unavailable": 1, "failed": 1}


def test_gzip_is_told_by_content_not_by_name(tmp_path: Path) -> None:
    with gzip.open(tmp_path / "plain-looking.jsonl", "wt", encoding="utf-8") as zipped:
        zipped.write(DAY_3)
    (tmp_path / "misnamed.gz").write_text(DAY_3, encoding="utf-8")
    report = querylog_report.Report()
    report.read(tmp_path / "plain-looking.jsonl")
    report.read(tmp_path / "misnamed.gz")
    assert (report.calls, report.broken) == (2, 8)


def test_the_text_report_opens_with_the_files_the_period_and_the_totals(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    text = run(capsys, *log_files)
    assert text.splitlines()[:5] == [
        f"Files: {', '.join(log_files)} (18 lines, 6 broken)",
        "Calls: 12  ok 7  error 1  refused 2  unavailable 1  failed 1",
        "Period: 2026-10-22 13:54:20 .. 2026-10-24 08:00:00 UTC",
        "Users: 3  Tools: 6  Stands: 4",
        "Duration: median 21.3 ms, 95th percentile 24990.0 ms",
    ]


def test_the_tools_are_listed_by_calls_with_their_share_of_problems(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert section(run(capsys, *log_files), "By tool") == [
        ["tool", "calls", "ok", "error", "refused", "unavailable", "failed", "problems"],
        ["run_select", "7", "4", "1", "2", "0", "0", "42.9%"],
        ["describe_table", "1", "1", "0", "0", "0", "0", "0.0%"],
        ["explain_query", "1", "1", "0", "0", "0", "0", "0.0%"],
        ["list_tables", "1", "0", "0", "0", "1", "0", "100.0%"],
        ["sample_rows", "1", "1", "0", "0", "0", "0", "0.0%"],
        ["status", "1", "0", "0", "0", "0", "1", "100.0%"],
    ]


def test_the_users_are_listed_with_their_queries_rows_and_refusals(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert section(run(capsys, *log_files), "By user") == [
        ["user", "calls", "queries", "rows", "refused"],
        ["anna", "7", "5", "33", "1"],
        ["boris", "4", "1", "20", "1"],
        ["(unknown)", "1", "0", "0", "0"],
    ]


def test_the_stands_count_single_calls_and_each_stand_of_a_call_over_several(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert section(run(capsys, *log_files), "By stand") == [
        ["stand", "calls", "ok", "error", "refused", "unavailable", "failed", "skipped", "problems"],
        ["dev7", "3", "2", "0", "1", "0", "0", "0", "33.3%"],
        ["dev1", "1", "1", "0", "0", "0", "0", "0", "0.0%"],
        ["dev2", "1", "0", "0", "0", "1", "0", "0", "100.0%"],
        ["dev3", "1", "0", "0", "0", "0", "0", "1", "0.0%"],  # skipped for want of time: no fault of the stand
    ]


def test_by_day_adds_a_table_by_day_to_the_text(log_files: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert "By day" not in run(capsys, *log_files)
    assert section(run(capsys, *log_files, "--by", "day"), "By day") == [
        ["day", "calls", "users", "ok", "error", "refused", "unavailable", "failed", "problems"],
        ["2026-10-22", "7", "3", "3", "1", "1", "1", "1", "57.1%"],
        ["2026-10-23", "4", "2", "3", "0", "1", "0", "0", "25.0%"],
        ["2026-10-24", "1", "1", "1", "0", "0", "0", "0", "0.0%"],
    ]


def test_the_longest_calls_come_first_and_the_refused_ones_in_order_of_time(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    text = run(capsys, *log_files)
    longest = section(text, "Longest calls (top 10)")
    assert longest[0] == ["duration_ms", "time", "user", "stand", "tool", "arguments"]
    durations = ["24990.0", "2000.0", "250.0", "100.0", "40.0", "30.0", "12.6", "7.0", "5.0", "1.0"]
    assert [row[0] for row in longest[1:]] == durations
    assert longest[1][1:8] == ["2026-10-23", "09:05:00", "anna", "3", "stands", "run_select", "stands=dev*"]
    refused = section(text, "Refused by the SQL check (2)")
    assert refused[1][:5] == ["2026-10-22", "13:54:20", "anna", "run_select", "query=DELETE"]
    assert refused[2][:6] == ["2026-10-23", "09:10:00", "boris", "dev7", "run_select", "stand=dev7"]


def test_sql_goes_on_one_line_cut_to_80_characters_and_passwords_are_hidden(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    text = run(capsys, *log_files)
    folded = (
        "stand=dev7 query=SELECT i.id, i.total FROM billing.invoices i JOIN billing.customers c ON c.id = i.customer_id"
    )
    assert folded[:79] + "…" in text
    assert folded not in text
    assert "stand=dev7 query=UPDATE billing.invoices SET paid = true password=***" in text
    assert "hunter2" not in text


def test_arguments_of_any_shape_are_rendered() -> None:
    render = querylog_report.render_arguments
    assert render({"table": "shop.orders", "rows": 20}) == "table=shop.orders rows=20"
    assert render({"query": "SELECT\n  1"}) == "query=SELECT 1"
    nested = {"api_token": "t", "nested": {"passwd": "p", "stand": "dev1"}}
    assert render(nested) == 'api_token=*** nested={"passwd": "***", "stand": "dev1"}'
    assert render(None) == "null"
    assert render(["a", 1]) == '["a", 1]'
    assert render({"query": "x" * 100}, width=20) == "query=" + "x" * 13 + "…"
    assert render({"query": "x" * 74}) == "query=" + "x" * 74  # exactly 80: nothing cut


def test_a_date_window_is_inclusive_at_the_start_and_a_bare_until_date_covers_its_day(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert "Calls: 5 " in run(capsys, *log_files, "--since", "2026-10-23")
    until = run(capsys, *log_files, "--until", "2026-10-22")
    assert "Filter: before 2026-10-23 00:00:00 UTC" in until
    assert "Calls: 7 " in until
    window = run(capsys, *log_files, "--since", "2026-10-22T14:00", "--until", "2026-10-22T15:00:00Z")
    assert "Filter: from 2026-10-22 14:00:00, before 2026-10-22 15:00:00 UTC" in window
    assert "Calls: 3 " in window  # 14:00:00 is in, 15:00:00 is out
    assert "Users: 2  Tools: 3" in window


def test_moments_on_the_command_line_are_utc_dates_or_date_times() -> None:
    parse = querylog_report.parse_moment
    assert parse("2026-10-22") == dt.datetime(2026, 10, 22, tzinfo=UTC)
    assert parse("2026-10-22", end_of_day=True) == dt.datetime(2026, 10, 23, tzinfo=UTC)
    assert parse("2026-10-22T14:30", end_of_day=True) == dt.datetime(2026, 10, 22, 14, 30, tzinfo=UTC)
    assert parse("2026-10-22 14:30:15+03:00") == dt.datetime(2026, 10, 22, 11, 30, 15, tzinfo=UTC)
    with pytest.raises(argparse.ArgumentTypeError, match="not a date or date-time: 2026-13-01"):
        parse("2026-13-01")


def test_times_in_the_log_are_read_as_utc() -> None:
    parse = querylog_report.parse_time
    assert parse("2026-10-22T13:54:20.570Z") == dt.datetime(2026, 10, 22, 13, 54, 20, 570000, tzinfo=UTC)
    assert parse("2026-10-22T13:54:20") == dt.datetime(2026, 10, 22, 13, 54, 20, tzinfo=UTC)
    assert parse("2026-10-22T16:54:20+03:00") == dt.datetime(2026, 10, 22, 13, 54, 20, tzinfo=UTC)
    assert parse("yesterday") is None
    assert parse(1761141260) is None


def test_the_percentile_is_the_nearest_rank() -> None:
    assert querylog_report.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5) == 3.0
    assert querylog_report.percentile([float(n) for n in range(1, 101)], 0.95) == 95.0
    assert querylog_report.percentile([10.0], 0.95) == 10.0


def test_a_line_is_broken_unless_it_has_time_tool_outcome_and_duration() -> None:
    parse = querylog_report.parse_entry
    assert parse("") is None
    assert parse("[]") is None
    assert parse('{"time": "2026-10-22T13:54:20Z", "tool": "status", "outcome": "ok", "duration_ms": true}') is None
    assert parse('{"time": "2026-10-22T13:54:20Z", "tool": 7, "outcome": "ok", "duration_ms": 1}') is None
    entry = parse(
        '{"time": "2026-10-22T13:54:20Z", "os_user": null, "tool": "status", "outcome": "ok", "duration_ms": 1}'
    )
    assert entry is not None
    assert (entry.user, entry.tool, entry.duration_ms, entry.rows, entry.stand, entry.stands) == (
        "(unknown)",
        "status",
        1.0,
        None,
        None,
        {},
    )
    assert entry.arguments == "null"


def test_json_carries_the_same_figures_with_times_in_iso_8601(
    log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    data = json.loads(run(capsys, *log_files, "--format", "json", "--since", "2026-10-22"))
    assert (data["files"], data["lines"], data["broken"], data["calls"]) == (log_files, 18, 6, 12)
    assert (data["since"], data["until"]) == ("2026-10-22T00:00:00.000Z", None)
    assert (data["first"], data["last"]) == ("2026-10-22T13:54:20.570Z", "2026-10-24T08:00:00.000Z")
    assert data["outcomes"] == {"ok": 7, "error": 1, "refused": 2, "unavailable": 1, "failed": 1}
    assert data["duration_ms"] == {"median": 21.3, "p95": 24990.0}
    assert data["tools"][0] == {
        "tool": "run_select",
        "calls": 7,
        "ok": 4,
        "error": 1,
        "refused": 2,
        "unavailable": 0,
        "failed": 0,
        "problems_pct": 42.9,
    }
    assert [row["user"] for row in data["users"]] == ["anna", "boris", "(unknown)"]
    assert [row["stand"] for row in data["stands"]] == ["dev7", "dev1", "dev2", "dev3"]
    assert [row["day"] for row in data["days"]] == ["2026-10-22", "2026-10-23", "2026-10-24"]
    assert (data["longest"][0]["stand"], data["longest"][0]["duration_ms"]) == ("3 stands", 24990.0)
    assert data["refused"][0] == {
        "time": "2026-10-22T13:54:20.571Z",
        "user": "anna",
        "stand": None,
        "tool": "run_select",
        "arguments": "query=DELETE FROM shop.orders WHERE status = 'cancelled'",
        "duration_ms": 0.0,
        "message": DELETE_REFUSAL,
    }
    assert data["refused"][1]["arguments"].endswith("password=***")


def test_csv_is_the_one_breakdown_that_by_chooses(log_files: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    users = list(csv.reader(io.StringIO(run(capsys, *log_files, "--format", "csv", "--by", "user"))))
    assert users == [
        ["user", "calls", "queries", "rows", "refused"],
        ["anna", "7", "5", "33", "1"],
        ["boris", "4", "1", "20", "1"],
        ["(unknown)", "1", "0", "0", "0"],
    ]
    days = list(csv.reader(io.StringIO(run(capsys, *log_files, "--format", "csv", "--by", "day"))))
    assert days[:2] == [
        ["day", "calls", "users", "ok", "error", "refused", "unavailable", "failed", "problems_pct"],
        ["2026-10-22", "7", "3", "3", "1", "1", "1", "1", "57.1"],
    ]
    tools = run(capsys, *log_files, "--format", "csv")  # --by tool unless said otherwise
    assert tools.startswith(
        "tool,calls,ok,error,refused,unavailable,failed,problems_pct\nrun_select,7,4,1,2,0,0,42.9\n"
    )


def test_without_stands_the_report_has_no_stand_column(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "queries.jsonl"
    path.write_text(DAY_1, encoding="utf-8")
    text = run(capsys, str(path))
    assert "Users: 3  Tools: 5\n" in text
    assert "Stands:" not in text
    assert "By stand" not in text
    assert section(text, "Longest calls (top 7)")[0] == ["duration_ms", "time", "user", "tool", "arguments"]
    assert section(text, "Refused by the SQL check (1)")[0] == ["time", "user", "tool", "arguments"]


def test_an_empty_log_reports_no_calls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "queries.jsonl"
    path.write_text("", encoding="utf-8")
    assert run(capsys, str(path)) == (
        f"Files: {path} (0 lines, 0 broken)\nCalls: 0  ok 0  error 0  refused 0  unavailable 0  failed 0\n"
    )
    assert json.loads(run(capsys, str(path), "--format", "json"))["duration_ms"] == {"median": None, "p95": None}
    header = "stand,calls,ok,error,refused,unavailable,failed,skipped,problems_pct\n"
    assert run(capsys, str(path), "--format", "csv", "--by", "stand") == header


def test_no_refusals_is_said_rather_than_shown_as_an_empty_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "queries.jsonl"
    path.write_text(DAY_3, encoding="utf-8")
    assert run(capsys, str(path)).endswith("Refused by the SQL check (0)\n  none\n")


@pytest.mark.parametrize(
    ("argv", "complaint"),
    [
        (["no-such-file.jsonl"], "cannot read no-such-file.jsonl"),
        (["--since", "2026-13-01"], "not a date or date-time: 2026-13-01"),
        (["--since", "2026-10-24", "--until", "2026-10-22"], "--since must be before --until"),
    ],
)
def test_bad_arguments_are_refused_with_a_reason(
    argv: list[str], complaint: str, log_files: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stop:
        querylog_report.main([*argv, log_files[0]] if argv[0].startswith("--") else argv)
    assert stop.value.code == 2
    assert complaint in capsys.readouterr().err


def test_a_gzip_file_cut_short_is_an_error_not_a_silent_half_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    whole = gzip.compress(DAY_1.encode("utf-8"))
    cut = tmp_path / "queries.jsonl.2.gz"
    cut.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(SystemExit):
        querylog_report.main([str(cut)])
    assert f"cannot read {cut}" in capsys.readouterr().err


def test_the_script_runs_on_its_own(log_files: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "querylog_report.py"), *log_files, "--by", "day"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Calls: 12  ok 7  error 1  refused 2  unavailable 1  failed 1" in proc.stdout
    assert "By day" in proc.stdout
