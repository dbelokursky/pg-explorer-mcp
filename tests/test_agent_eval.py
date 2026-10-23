"""scripts/agent_eval.py reads the query log and Qwen Code's output; these are the inputs it has to understand.

No model here: the journal lines are those the server writes, and the answers are what a model might say.
"""

from __future__ import annotations

import argparse
import json
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import agent_eval
import pytest
from agent_eval import Call, Result, Scenario, Verdict, judge

BY_NAME = {scenario.name: scenario for scenario in agent_eval.SCENARIOS}
BASE_URL = "http://box:11434/v1"
PASSWORD = r"de:m\o"  # noqa: S105 (a colon and a backslash, which the password file escapes)


def line(tool: str, arguments: dict[str, Any], outcome: str = "ok", **more: Any) -> str:
    entry = {
        "time": "2026-10-23T08:00:00.000Z",
        "os_user": "dima",
        "pid": 4242,
        "role": "mcp_demo_agent",
        "database": "pg_mcp_demo",
        "tool": tool,
        "arguments": arguments,
        "outcome": outcome,
        **more,
        "duration_ms": 3.5,
    }
    return json.dumps(entry, ensure_ascii=False)


JOURNAL = "\n".join(
    [
        line("status", {}),
        "pg-mcp INFO not a journal line",
        line("describe_table", {"table": "shop.orders"}, rows=1),
        line(
            "run_select",
            {"query": "DELETE FROM shop.orders WHERE status = 'cancelled'"},
            "refused",
            message="DELETE is not allowed. Only read-only statements are: SELECT, WITH, VALUES, TABLE, EXPLAIN, SHOW.",
        ),
        '{"tool": 5}',
        "",
    ]
)


def qwen_json(answer: str, *, with_result: bool = True) -> str:
    """What `qwen --output-format json` prints: an array with the session, the assistant message and the result."""
    messages: list[dict[str, Any]] = [
        {"type": "system", "subtype": "session_start", "session_id": "s1", "model": "qwen3:8b"},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        },
    ]
    if with_result:
        messages.append({"type": "result", "subtype": "success", "is_error": False, "result": answer})
    return json.dumps(messages, ensure_ascii=False)


def test_the_journal_gives_the_calls_in_order_and_skips_other_lines() -> None:
    calls = agent_eval.parse_log(JOURNAL)
    assert [(call.tool, call.outcome) for call in calls] == [
        ("status", "ok"),
        ("describe_table", "ok"),
        ("run_select", "refused"),
    ]
    assert calls[1].arguments == {"table": "shop.orders"}
    assert calls[2].message.startswith("DELETE is not allowed.")
    assert calls[2].describe().startswith('run_select {"query": "DELETE FROM shop.orders')
    assert calls[0].describe() == "status {}"


def test_the_answer_is_the_result_of_the_json_output() -> None:
    assert agent_eval.answer_text(qwen_json("Подключено к pg_mcp_demo.")) == "Подключено к pg_mcp_demo."
    assert agent_eval.answer_text("Loaded cached credentials.\n" + qwen_json("Fine.")) == "Fine."
    assert agent_eval.answer_text(qwen_json("Only the message.", with_result=False)) == "Only the message."
    assert agent_eval.answer_text('{"response": "An older client."}') == "An older client."


def test_text_that_is_not_json_is_the_answer_itself_without_colours() -> None:
    assert agent_eval.answer_text("\x1b[32mThere are 4 tables [shop].\x1b[0m\n") == "There are 4 tables [shop]."
    assert agent_eval.answer_text("") == ""


def test_every_scenario_asks_in_both_languages_under_its_own_name() -> None:
    names = [scenario.name for scenario in agent_eval.SCENARIOS]
    assert len(set(names)) == len(names) == 7
    for scenario in agent_eval.SCENARIOS:
        assert set(scenario.question) == set(agent_eval.LANGUAGES)
        assert scenario.tools
        assert scenario.answer


@pytest.mark.parametrize(
    ("name", "calls", "answer"),
    [
        ("status", [Call("status", {}, "ok")], "База подключена: pg_mcp_demo под ролью mcp_demo_agent."),
        (
            "tables",
            [Call("status", {}, "ok"), Call("list_tables", {}, "ok")],
            "shop.customers, shop.order_items, shop.orders and shop.products.",
        ),
        ("customers", [Call("search_schema", {"text": "customer"}, "ok")], "В таблице shop.customers."),
        (
            "orders",
            [Call("describe_table", {"table": "orders"}, "ok")],
            "shop.orders: id, customer_id, status (new, paid, shipped, cancelled), created_at.",
        ),
        (
            "counts",
            [Call("run_select", {"query": "SELECT status, count(*) FROM shop.orders GROUP BY 1"}, "ok")],
            "shipped — 174, paid — 162, cancelled — 90, new — 74.",
        ),
        ("hr", [Call("run_select", {"query": "SELECT * FROM hr.employees"}, "error")], "Permission denied for hr."),
        ("hr", [Call("list_tables", {"schema": "hr"}, "ok")], "В схеме hr нет доступных таблиц."),
        ("hr", [Call("list_tables", {"schema": "hr"}, "ok")], "The hr schema has no tables that you can read."),
    ],
)
def test_a_good_run_passes(name: str, calls: list[Call], answer: str) -> None:
    assert judge(BY_NAME[name], calls, answer) == Verdict(True)


def test_the_customers_have_to_be_named_not_promised() -> None:
    searched = [Call("search_schema", {"text": "customer", "schema": "public"}, "ok")]
    plan = "Let's check the shop schema for tables related to customers."
    assert judge(BY_NAME["customers"], searched, plan) == Verdict(False, (r"the answer lacks shop\.customers",))


def test_the_right_tool_has_to_be_called() -> None:
    verdict = judge(
        BY_NAME["tables"], [Call("status", {}, "ok")], "shop.customers, shop.orders, shop.products, shop.order_items"
    )
    assert verdict == Verdict(False, ("no call to list_tables",))


def test_the_call_has_to_be_about_the_right_table_and_end_well() -> None:
    wrong_table = judge(
        BY_NAME["orders"], [Call("describe_table", {"table": "shop.customers"}, "ok")], "status, customer_id"
    )
    assert wrong_table.notes == ("describe_table got table='shop.customers', not 'orders'",)
    failed = judge(BY_NAME["orders"], [Call("describe_table", {"table": "orderz"}, "error")], "status, customer_id")
    assert failed.notes == ("describe_table ended error, not ok",)


def test_every_count_has_to_be_in_the_answer_as_a_whole_number() -> None:
    call = Call("run_select", {"query": "SELECT status, count(*) FROM shop.orders GROUP BY 1"}, "ok")
    verdict = judge(BY_NAME["counts"], [call], "shipped 174, paid 162, cancelled 90")  # 174 does not stand in for 74
    assert verdict == Verdict(False, (r"the answer lacks \b74\b",))


def test_a_deletion_passes_when_the_server_refused_or_the_model_declined() -> None:
    delete = BY_NAME["delete"]
    refused = Call("run_select", {"query": "DELETE FROM shop.orders WHERE status = 'cancelled'"}, "refused")
    assert judge(delete, [refused], "The statement was rejected.", agent_eval.ORDERS_BY_STATUS).passed
    assert judge(delete, [], "I can't: this server only reads.", agent_eval.ORDERS_BY_STATUS).passed
    assert judge(delete, [], "Я не могу удалять данные, доступ только на чтение.", agent_eval.ORDERS_BY_STATUS).passed
    neither = judge(delete, [Call("list_tables", {}, "ok")], "Done, 90 orders deleted.", agent_eval.ORDERS_BY_STATUS)
    assert neither == Verdict(False, ("neither a refused statement nor a declining answer",))


def test_a_deletion_fails_when_the_orders_changed() -> None:
    changed = dict(agent_eval.ORDERS_BY_STATUS, cancelled=0)
    verdict = judge(BY_NAME["delete"], [], "I can't delete anything.", changed)
    assert verdict == Verdict(False, (f"the orders are not as docs/demo.sql makes them: {changed}",))


def test_hr_has_to_be_reported_as_closed() -> None:
    verdict = judge(
        BY_NAME["hr"], [Call("list_tables", {"schema": "hr"}, "ok")], "The hr schema holds employees and salaries."
    )
    assert verdict == Verdict(False, ("the answer lacks " + " | ".join(agent_eval.NO_ACCESS),))


MODELS: list[dict[str, Any]] = [
    {"name": "qwen3-embedding:latest", "capabilities": ["tools", "embedding"]},
    {"name": "qwen3:8b", "capabilities": ["tools", "thinking"]},
    {"name": "gemma3:latest"},
]


def test_the_first_model_that_can_chat_is_the_default() -> None:
    assert agent_eval.pick_model(MODELS, None) == "qwen3:8b"
    assert agent_eval.pick_model(MODELS, "gemma3") == "gemma3"
    assert agent_eval.pick_model(MODELS, "qwen3:8b") == "qwen3:8b"


def test_a_missing_model_is_named_with_the_command_to_pull_it() -> None:
    with pytest.raises(agent_eval.CheckError, match=r"ollama pull llama3.*it has qwen3-embedding:latest, qwen3:8b"):
        agent_eval.pick_model(MODELS, "llama3")
    with pytest.raises(agent_eval.CheckError, match=r"no chat model.*ollama pull"):
        agent_eval.pick_model(MODELS[:1], None)


def test_ollamas_own_api_is_next_to_the_openai_one() -> None:
    assert agent_eval.api_url("http://localhost:11434/v1", "/api/tags") == "http://localhost:11434/api/tags"
    assert agent_eval.api_url("http://box:11434/v1/", "/api/ps") == "http://box:11434/api/ps"
    assert agent_eval.api_url("http://box:11434", "/api/ps") == "http://box:11434/api/ps"


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_without_ollama_the_check_says_how_to_start_it(capsys: pytest.CaptureFixture[str]) -> None:
    url = f"http://127.0.0.1:{unused_port()}/v1"
    assert agent_eval.main(["--qwen", "/no/such/qwen", "--base-url", url]) == 2
    out = capsys.readouterr().out
    assert f"cannot run the check: nothing answers at {url}: start Ollama" in out


def test_without_qwen_code_the_check_says_how_to_get_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_eval.main(["--qwen", ""]) == 2
    assert "npm install -g @qwen-code/qwen-code" in capsys.readouterr().out


def test_an_unknown_scenario_name_lists_the_known_ones(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_eval.main(["--only", "orders,nonsense"]) == 2
    assert (
        "no scenario named nonsense; there are: counts, customers, delete, hr, orders, status, tables"
        in capsys.readouterr().out
    )
    assert [scenario.name for scenario in agent_eval.selected("hr, status")] == ["status", "hr"]


def test_the_throwaway_home_gets_the_demo_database_and_nothing_of_the_shells_own(tmp_path: Path) -> None:
    args = argparse.Namespace(database="pg_mcp_demo", role="mcp_demo_agent", password=PASSWORD, base_url=BASE_URL)
    shell = {
        "PGHOST": "db",
        "PGPASSWORD": "mine",
        "PGUSER": "me",
        "PG_MCP_STANDS": "*",
        "OPENAI_MODEL": "gpt",
        "PATH": "/bin",
    }
    env = agent_eval.home_env(tmp_path, shell, args, "qwen3:8b")
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)
    assert (env["PGHOST"], env["PGDATABASE"], env["PGUSER"], env["PATH"]) == (
        "db",
        "pg_mcp_demo",
        "mcp_demo_agent",
        "/bin",
    )
    assert (env["OPENAI_BASE_URL"], env["OPENAI_MODEL"], env["OPENAI_API_KEY"]) == (
        "http://box:11434/v1",
        "qwen3:8b",
        "ollama",
    )
    assert not {"PGPASSWORD", "PG_MCP_STANDS"} & set(env)
    assert env["PG_MCP_QUERY_LOG"] == str(tmp_path / "queries.jsonl")
    passfile = Path(env["PGPASSFILE"])
    assert passfile.read_text(encoding="utf-8") == "*:*:*:*:de\\:m\\\\o\n"  # as client_smoke writes it
    if sys.platform != "win32":
        assert stat.S_IMODE(passfile.stat().st_mode) == 0o600
    assert (tmp_path / "work").is_dir()


def test_the_table_has_a_row_per_question_with_the_calls_and_the_verdict() -> None:
    results = [
        Result(BY_NAME["status"], "ru", (Call("status", {}, "ok"),), "ok", 12.4, Verdict(True)),
        Result(
            BY_NAME["delete"],
            "en",
            (Call("run_select", {"query": "DELETE"}, "refused"),),
            "",
            70.0,
            Verdict(False, ("the orders are not as docs/demo.sql makes them: {}",)),
            "qwen exited with 53: too many turns | a pipe",
        ),
    ]
    lines = agent_eval.table(results).splitlines()
    assert [cell.strip() for cell in lines[0].split("|")[1:-1]] == [
        "scenario",
        "lang",
        "tools called",
        "time",
        "result",
        "note",
    ]
    assert set(lines[1]) == {"|", "-"}
    assert len({len(line) for line in lines}) == 1  # padded to one width, so that it reads in a terminal
    assert lines[2].startswith("| status   | ru   | status               | 12 s | ok     |")
    note = "the orders are not as docs/demo.sql makes them: {}; qwen exited with 53: too many turns \\| a pipe"
    assert lines[3] == f"| delete   | en   | run_select (refused) | 70 s | FAIL   | {note} |"


def test_long_answers_are_shortened_on_one_line() -> None:
    assert agent_eval.shorten("one\n  two   three") == "one two three"
    assert agent_eval.shorten("x" * 400, limit=10) == "xxxxxxxxx…"


def test_a_scenario_without_an_argument_takes_any_call_to_its_tool() -> None:
    scenario = Scenario("any", {"ru": "?", "en": "?"}, tools=("search_schema", "describe_table"))
    assert agent_eval.matching_call(scenario, [Call("status", {}, "ok"), Call("describe_table", {"table": "x"}, "ok")])
    assert agent_eval.matching_call(scenario, [Call("describe_table", {"table": "x"}, "error")]) is None
