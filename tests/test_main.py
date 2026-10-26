"""main() in-process, with byte streams instead of stdin and stdout."""

from __future__ import annotations

import io
import json
import logging

import pytest

from pg_explorer_mcp import __main__ as entry
from tests.helpers import ROOT, rpc


def test_main_answers_every_request_until_input_ends(unreachable_env: dict[str, str]) -> None:
    requests = [
        rpc(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc(2, "tools/call", {"name": "status", "arguments": {}}),
    ]
    stdin = io.BytesIO(b"\n".join(json.dumps(request).encode() for request in requests) + b"\n\n")
    stdout = io.BytesIO()
    assert entry.main(stdin, stdout, env=unreachable_env) == 0
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2]
    assert "database: unavailable" in replies[1]["result"]["content"][0]["text"]


def test_checkout_is_reported_as_the_install_dir() -> None:
    assert str(ROOT) == entry.INSTALL_DIR


def status_from_main(env: dict[str, str]) -> str:
    stdin = io.BytesIO(json.dumps(rpc(1, "tools/call", {"name": "status", "arguments": {}})).encode() + b"\n")
    stdout = io.BytesIO()
    assert entry.main(stdin, stdout, env=env) == 0
    return str(json.loads(stdout.getvalue())["result"]["content"][0]["text"])


def test_main_speaks_the_language_pg_explorer_mcp_lang_names(unreachable_env: dict[str, str]) -> None:
    text = status_from_main(dict(unreachable_env, PG_EXPLORER_MCP_LANG="ru"))
    assert "язык: ru" in text
    assert "база: недоступна" in text


def test_unknown_language_falls_back_to_english_with_a_warning(unreachable_env: dict[str, str]) -> None:
    text = status_from_main(dict(unreachable_env, PG_EXPLORER_MCP_LANG="de", PG_EXPLORER_MCP_PROFILE="admin"))
    assert "language: en" in text
    assert "warning: PG_EXPLORER_MCP_LANG='de' is not one of en, ru; using 'en'" in text
    assert "warning: PG_EXPLORER_MCP_PROFILE='admin'" in text


def test_status_names_the_server_entry(unreachable_env: dict[str, str]) -> None:
    assert "server name: pg" in status_from_main(unreachable_env).splitlines()
    assert (
        "server name: pg-sales" in status_from_main(dict(unreachable_env, PG_EXPLORER_MCP_NAME="pg-sales")).splitlines()
    )


def test_settings_written_by_pg_mcp_0_7_still_work_and_the_log_says_so(
    unreachable_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    env = {key: value for key, value in unreachable_env.items() if key != "PG_EXPLORER_MCP_LANG"}
    with caplog.at_level(logging.INFO):
        text = status_from_main(dict(env, PG_MCP_NAME="pg-sales", PG_MCP_LANG="ru"))
    assert "имя сервера: pg-sales" in text.splitlines()
    assert "язык: ru" in text
    assert "old setting names PG_MCP_LANG, PG_MCP_NAME; install.py writes the new ones" in caplog.text


def test_a_new_setting_name_wins_over_its_old_twin(unreachable_env: dict[str, str]) -> None:
    text = status_from_main(dict(unreachable_env, PG_MCP_NAME="pg-old", PG_EXPLORER_MCP_NAME="pg-new"))
    assert "server name: pg-new" in text.splitlines()
