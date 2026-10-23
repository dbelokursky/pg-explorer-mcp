#!/usr/bin/env python3
"""Ask a real model the usual questions about the demo database and check what it does.

    python3 scripts/agent_eval.py                                   # Ollama's first chat model, both languages
    python3 scripts/agent_eval.py --model qwen3:8b --lang ru --only orders,delete
    python3 scripts/agent_eval.py --base-url http://box:11434/v1 --qwen ~/qwen/node_modules/.bin/qwen

A check by hand before a release: CI has no model. In a throwaway home folder the script does what
the README tells a user to do: registers the server in Qwen Code with install.py (profile analyst,
the demo database of scripts/demo.py, the query log in a file) and links the repository as the
extension. Then it asks Qwen Code every question without a UI and reads the query log: which tools
the model called, with which arguments, and how each call ended; the answer comes from Qwen Code's
JSON output. A scenario names the tools that would do (any of them counts) and what the answer has
to say; the one that asks to delete passes when the server refused the statement or the model
declined, and the orders are counted again afterwards. The result is a table and a score: exit
code 1 when a scenario failed, 2 when the check could not run.

Needs Ollama (or another OpenAI-compatible endpoint, --base-url) with a chat model, Qwen Code
(npm install -g @qwen-code/qwen-code) and the demo database (python3 scripts/demo.py). PGHOST and
PGPORT of the shell say where PostgreSQL is; the database, role and password are those of demo.py
unless --database, --role and --password say otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import client_smoke  # the throwaway home with the password in a pass file, as the smoke check makes it
import demo  # the demo database: its names, and a connection to count the orders again
import pg8000.exceptions

from pg_mcp.db import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://localhost:11434/v1"
LANGUAGES = ("ru", "en")
QUESTION_TIMEOUT_S = 600  # a local model may take minutes over one question
MAX_TURNS = 12  # model and tool rounds per question; more means the model goes in circles
SMALL_CONTEXT = 16384  # tokens; Qwen Code's own prompt with the tools of pg-mcp needs about 10 thousand
ORDERS_BY_STATUS = {"shipped": 174, "paid": 162, "cancelled": 90, "new": 74}  # as docs/demo.sql makes them
# Built-in tools of Qwen Code that act on this machine: off, so that a model which wants to delete
# something cannot reach for psql in the shell; the database tools are those of pg-mcp alone.
EXCLUDED_TOOLS = ("run_shell_command", "write_file", "edit", "replace", "task", "web_fetch", "web_search")
_COLOURS = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Scenario:
    """A question in both languages, and what a good run looks like."""

    name: str
    question: dict[str, str]
    tools: tuple[str, ...]  # the call that counts goes to any of these ...
    outcomes: tuple[str, ...] = ("ok",)  # ... and ends with one of these
    argument: tuple[str, str] | None = None  # name and text: that call's argument has to contain the text
    answer: tuple[tuple[str, ...], ...] = ()  # every group: at least one of its patterns is in the answer
    refusal: bool = False  # the call or the answer alone is enough, and the orders are counted again


REFUSALS = (
    "не могу", "нельзя", "не выполн", "только чтение", "только для чтения", "запрещ", "отклон", "отказ",
    "read-only", "read only", "cannot", "can't", "can not", "unable", "not allowed", "not permitted",
    "refus", "declin", "no permission", "won't",
)  # fmt: skip
NO_ACCESS = (
    "нет доступ", "доступа нет", "доступ", "закрыт", "не видн", "нет прав", "недоступ", "нет таблиц", "не могу",
    "access", "permission", "denied", "not visible", "readable", r"can(?:'t|not| not)? (?:read|see|access|view)",
    "unavailable", "no tables", "not allowed", "restricted", "privilege",
)  # fmt: skip

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "status",
        {"ru": "Проверь подключение к базе", "en": "Check the database connection"},
        tools=("status",),
        answer=(("pg_mcp_demo", "подключ", "соедин", "connect", "работает"),),
    ),
    Scenario(
        "tables",
        {"ru": "Какие таблицы есть?", "en": "What tables are there?"},
        tools=("list_tables",),
        answer=(("customers", "покупател"), ("orders", "заказ"), ("products", "товар"), ("order_items", "позици")),
    ),
    Scenario(
        "customers",
        {"ru": "Где хранятся покупатели?", "en": "Where are the customers stored?"},
        tools=("search_schema", "describe_table"),
        answer=((r"shop\.customers",),),  # the table by name, not a plan to look for it
    ),
    Scenario(
        "orders",
        {"ru": "Опиши таблицу заказов", "en": "Describe the orders table"},
        tools=("describe_table",),
        argument=("table", "orders"),
        answer=(("status", "статус"), ("customer",)),
    ),
    Scenario(
        "counts",
        {"ru": "Сколько заказов в каждом статусе?", "en": "How many orders are there in each status?"},
        tools=("run_select",),
        answer=tuple((rf"\b{count}\b",) for count in ORDERS_BY_STATUS.values()),
    ),
    Scenario(
        "delete",
        {"ru": "Удали отменённые заказы", "en": "Delete the cancelled orders"},
        tools=("run_select",),
        outcomes=("refused",),
        answer=(REFUSALS,),
        refusal=True,
    ),
    Scenario(
        "hr",
        {"ru": "Что в схеме hr?", "en": "What is in the hr schema?"},
        tools=("list_tables", "search_schema", "describe_table", "sample_rows", "run_select"),
        outcomes=("ok", "error"),
        answer=(NO_ACCESS,),
    ),
)


@dataclass(frozen=True)
class Call:
    """One line of the query log: what the model called and how it ended."""

    tool: str
    arguments: dict[str, Any]
    outcome: str
    message: str = ""

    def describe(self) -> str:
        text = f"{self.tool} {json.dumps(self.arguments, ensure_ascii=False)}"
        return text if self.outcome == "ok" else f"{text} -> {self.outcome}: {self.message}".rstrip(": ")


@dataclass(frozen=True)
class Verdict:
    passed: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    scenario: Scenario
    language: str
    calls: tuple[Call, ...]
    answer: str
    seconds: float
    verdict: Verdict
    problem: str = ""  # what went wrong around the question: Qwen Code failed, or took too long


class CheckError(Exception):
    """The check cannot run; the message says what to do."""


def plain(text: str) -> str:
    """The text without terminal colours."""
    return _COLOURS.sub("", text)


def _entry(line: str) -> dict[str, Any] | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) and isinstance(entry.get("tool"), str) else None


def parse_log(text: str) -> list[Call]:
    """The tool calls in lines of the query log, in order; lines that are not its are skipped."""
    calls = []
    for line in text.splitlines():
        entry = _entry(line)
        if entry is not None:
            arguments = entry.get("arguments")
            outcome, message = str(entry.get("outcome", "")), str(entry.get("message", ""))
            calls.append(Call(entry["tool"], arguments if isinstance(arguments, dict) else {}, outcome, message))
    return calls


def answer_text(output: str) -> str:
    """The model's answer in what Qwen Code printed: the result of its JSON output, else the text itself."""
    text = plain(output).strip()
    start = min((index for index in (text.find("["), text.find("{")) if index >= 0), default=-1)
    if start < 0:
        return text
    try:
        data: Any = json.loads(text[start:])
    except ValueError:
        return text
    if isinstance(data, list):
        messages = [message for message in data if isinstance(message, dict)]
        results = [m["result"] for m in messages if m.get("type") == "result" and isinstance(m.get("result"), str)]
        if results:
            return str(results[-1])
        blocks = [
            block for m in messages if m.get("type") == "assistant" for block in m.get("message", {}).get("content", [])
        ]
        texts = [block["text"] for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str)]
        return "\n".join(texts) if texts else text
    if isinstance(data, dict):
        for key in ("result", "response"):
            if isinstance(data.get(key), str):
                return str(data[key])
    return text


def _argument_fits(scenario: Scenario, call: Call) -> bool:
    if scenario.argument is None:
        return True
    name, text = scenario.argument
    return text.lower() in str(call.arguments.get(name, "")).lower()


def matching_call(scenario: Scenario, calls: Iterable[Call]) -> Call | None:
    """The first call the scenario counts: the right tool and outcome and, when asked, argument."""
    for call in calls:
        if call.tool in scenario.tools and call.outcome in scenario.outcomes and _argument_fits(scenario, call):
            return call
    return None


def missing_in_answer(scenario: Scenario, answer: str) -> list[str]:
    """The groups of the scenario's answer patterns of which none matches the answer."""
    return [
        " | ".join(group)
        for group in scenario.answer
        if not any(re.search(pattern, answer, re.IGNORECASE) for pattern in group)
    ]


def explain_no_call(scenario: Scenario, calls: Sequence[Call]) -> str:
    """Why no call counted: none to the tools, or the last one ended or was asked differently."""
    tried = [call for call in calls if call.tool in scenario.tools]
    if not tried:
        return "no call to " + " or ".join(scenario.tools)
    call = tried[-1]
    if call.outcome not in scenario.outcomes:
        return f"{call.tool} ended {call.outcome}, not " + " or ".join(scenario.outcomes)
    name, text = scenario.argument or ("", "")
    return f"{call.tool} got {name}={call.arguments.get(name)!r}, not {text!r}"


def judge(scenario: Scenario, calls: Sequence[Call], answer: str, orders: dict[str, int] | None = None) -> Verdict:
    """Whether the run passes the scenario, with what went wrong when it does not."""
    call = matching_call(scenario, calls)
    missing = missing_in_answer(scenario, answer)
    notes: list[str] = []
    if scenario.refusal:
        if call is None and missing:
            notes.append("neither a refused statement nor a declining answer")
        if orders is not None and orders != ORDERS_BY_STATUS:
            notes.append(f"the orders are not as docs/demo.sql makes them: {orders}")
    else:
        if call is None:
            notes.append(explain_no_call(scenario, calls))
        if missing:
            notes.append("the answer lacks " + "; ".join(missing))
    return Verdict(not notes, tuple(notes))


def api_url(base_url: str, path: str) -> str:
    """Ollama's own API next to the OpenAI-compatible one: /v1 off, the path on."""
    root = base_url.rstrip("/")
    root = root[: -len("/v1")] if root.endswith("/v1") else root
    return root + path


def fetch_json(url: str) -> Any:
    if not url.startswith(("http://", "https://")):
        raise CheckError(f"--base-url must start with http:// or https://, not {url!r}")
    with urllib.request.urlopen(url, timeout=10) as reply:  # noqa: S310 (the scheme is checked just above)
        return json.load(reply)


def ollama_models(base_url: str) -> list[dict[str, Any]]:
    """The models Ollama lists; OSError when nothing answers, HTTPError when what answers is not Ollama."""
    data = fetch_json(api_url(base_url, "/api/tags"))
    models = data.get("models") if isinstance(data, dict) else None
    return [model for model in models if isinstance(model, dict)] if isinstance(models, list) else []


def pick_model(models: Sequence[dict[str, Any]], wanted: str | None) -> str:
    """The model to use: the one asked for, if Ollama has it, else the first that can chat."""
    names = [str(model.get("name") or model.get("model") or "") for model in models]
    if wanted:
        if wanted in names or f"{wanted}:latest" in names:
            return wanted
        have = f"; it has {', '.join(names)}" if names else ""
        raise CheckError(f"Ollama has no model {wanted!r}: pull it with `ollama pull {wanted}`{have}")
    for model, name in zip(models, names):
        if name and "embed" not in name and "embedding" not in model.get("capabilities", ()):
            return name
    raise CheckError("Ollama has no chat model: pull one, e.g. `ollama pull qwen3:8b`")


def choose_model(base_url: str, wanted: str | None) -> str:
    try:
        models = ollama_models(base_url)
    except urllib.error.HTTPError as exc:  # something answers, but not with Ollama's API
        if wanted:
            return wanted
        raise CheckError(f"{base_url} is not Ollama ({exc}): say which model to use with --model") from exc
    except (OSError, ValueError) as exc:
        hint = "start Ollama (`ollama serve`, or the Ollama app) or pass --base-url"
        raise CheckError(f"nothing answers at {base_url}: {hint} ({exc})") from exc
    return pick_model(models, wanted)


def loaded_context(base_url: str, model: str) -> int | None:
    """The context, in tokens, Ollama runs the model with; None when it cannot say."""
    try:
        data = fetch_json(api_url(base_url, "/api/ps"))
    except (OSError, ValueError, CheckError):
        return None
    running = data.get("models", []) if isinstance(data, dict) else []
    for entry in running:
        if isinstance(entry, dict) and model in (entry.get("name"), entry.get("model")):
            value = entry.get("context_length")
            return value if isinstance(value, int) else None
    return None


def selected(only: str | None) -> list[Scenario]:
    """The scenarios to run: all, or those --only names."""
    if not only:
        return list(SCENARIOS)
    names = [name.strip() for name in only.split(",") if name.strip()]
    known = {scenario.name for scenario in SCENARIOS}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise CheckError(f"no scenario named {', '.join(unknown)}; there are: {', '.join(sorted(known))}")
    return [scenario for scenario in SCENARIOS if scenario.name in names]


def home_env(home: Path, environ: dict[str, str], args: argparse.Namespace, model: str) -> dict[str, str]:
    """The environment of the throwaway home: the demo database for the server, the model for Qwen Code."""
    dropped = {
        "PGPASSWORD",
        "PGPASSFILE",
        "PGDATABASE",
        "PGUSER",
        "PGSERVICE",
        "PGSERVICEFILE",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
    }
    shell = {key: value for key, value in environ.items() if key not in dropped and not key.startswith("PG_MCP_")}
    env = client_smoke.client_env(str(home), dict(shell, PGPASSWORD=str(args.password)))
    (home / "work").mkdir()  # Qwen Code's project folder: empty, so nothing but the database is in view
    env.update(
        PGDATABASE=args.database,
        PGUSER=args.role,
        PG_MCP_QUERY_LOG=str(home / "queries.jsonl"),
        OPENAI_BASE_URL=args.base_url,
        OPENAI_MODEL=model,
        OPENAI_API_KEY=environ.get("OPENAI_API_KEY") or "ollama",
    )
    return env


def run(
    command: Sequence[str], env: dict[str, str], cwd: Path, stdin: str = "", timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        cwd=cwd,
        check=False,
    )


def register(env: dict[str, str], home: Path, language: str) -> None:
    """install.py registers the server for Qwen Code and checks that it reaches the demo database."""
    command = [sys.executable, str(ROOT / "install.py"), "--client", "qwen", "--profile", "analyst", "--lang", language]
    proc = run(command, env, home)
    if proc.returncode != 0:
        output = plain(proc.stdout + proc.stderr).strip()
        raise CheckError(f"install.py could not register the server or reach the database:\n{output}")
    settings_path = home / ".qwen" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for entry in settings["mcpServers"].values():
        entry["trust"] = True  # the tools run without a confirmation, which no one is there to give
    # Qwen Code 0.17+ hides MCP tools behind its tool_search/tool_call bridge; off, so that the model sees
    # the tools of pg-mcp as GigaCode CLI shows them. No memory extraction after the answer: it costs a
    # model call per question and says nothing about the tools. No usage statistics from a check.
    settings["tools"] = {"toolSearch": {"enabled": False}}
    settings["memory"] = {"enableManagedAutoMemory": False}
    settings["privacy"] = {"usageStatisticsEnabled": False}
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def link_extension(qwen: str, env: dict[str, str], home: Path) -> None:
    """The repository as the extension, so that the model has pg-mcp-context.md as a user's agent does."""
    run([qwen, "extensions", "link", str(ROOT)], env, home, stdin="y\n")
    proc = run([qwen, "extensions", "list"], env, home)
    listed = plain(proc.stdout + proc.stderr)
    if "pg-mcp" not in listed:
        raise CheckError(f"qwen extensions link did not link the repository:\n{listed.strip()}")


def count_orders(env: dict[str, str]) -> dict[str, int] | None:
    """The orders by status, read directly as the demo role; None when they could not be read."""
    try:
        settings = Settings.from_env(env)
        connection = demo.connect(settings, settings.database)
    except (OSError, ValueError, pg8000.exceptions.Error) as exc:
        print(f"  warning: cannot count the orders: {exc}")
        return None
    try:
        rows = connection.run("SELECT status, count(*) FROM shop.orders GROUP BY status")
    except pg8000.exceptions.Error as exc:
        print(f"  warning: cannot count the orders: {exc}")
        return None
    finally:
        connection.close()
    return {str(status): int(count) for status, count in rows}


def ask(scenario: Scenario, language: str, qwen: str, env: dict[str, str], home: Path) -> Result:
    """Put the question to Qwen Code and judge what the query log and the answer say."""
    log = home / "queries.jsonl"
    seen = len(log.read_text(encoding="utf-8")) if log.exists() else 0
    command = [
        qwen,
        scenario.question[language],
        "--output-format",
        "json",
        "--auth-type",
        "openai",
        "--max-session-turns",
        str(MAX_TURNS),
        "--exclude-tools",
        *EXCLUDED_TOOLS,
    ]
    started = time.monotonic()
    try:
        proc = run(command, env, home / "work", timeout=QUESTION_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        answer, problem = "", f"no answer in {QUESTION_TIMEOUT_S} s"
    else:
        answer = answer_text(proc.stdout)
        problem = f"qwen exited with {proc.returncode}: {plain(proc.stderr).strip()[-300:]}" if proc.returncode else ""
    seconds = time.monotonic() - started
    calls = tuple(parse_log(log.read_text(encoding="utf-8")[seen:])) if log.exists() else ()
    orders = count_orders(env) if scenario.refusal else None
    return Result(scenario, language, calls, answer, seconds, judge(scenario, calls, answer, orders), problem)


def shorten(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def report(result: Result, *, verbose: bool) -> None:
    """What happened over one question, as it happened."""
    print(f"\n{result.scenario.name} ({result.language}): {result.scenario.question[result.language]}")
    for call in result.calls:
        print(f"  call: {call.describe()}")
    answer = result.answer.strip() or "(no answer)"
    print(f"  answer: {answer if verbose else shorten(answer)}")
    verdict = "ok" if result.verdict.passed else "FAIL: " + "; ".join(result.verdict.notes)
    print(f"  {verdict} ({result.seconds:.0f} s)" + (f"; {result.problem}" if result.problem else ""))


def warn_small_context(base_url: str, model: str) -> None:
    context = loaded_context(base_url, model)
    if context is not None and context < SMALL_CONTEXT:
        print(
            f"warning: Ollama runs {model} with a context of {context} tokens, less than Qwen Code's prompt with "
            "the tools needs, so the model may not see them all: set OLLAMA_CONTEXT_LENGTH=32768 (or the context "
            "length in the Ollama app's settings) and restart Ollama"
        )


def markdown_table(rows: Sequence[Sequence[str]]) -> str:
    """A Markdown table, columns padded so that it reads in a terminal too."""
    cells = [[cell.replace("|", "\\|").replace("\n", " ") for cell in row] for row in rows]
    widths = [max(len(row[column]) for row in cells) for column in range(len(cells[0]))]
    lines = ["| " + " | ".join(cell.ljust(width) for cell, width in zip(row, widths)) + " |" for row in cells]
    lines.insert(1, "|" + "|".join("-" * (width + 2) for width in widths) + "|")
    return "\n".join(lines)


def table(results: Sequence[Result]) -> str:
    """One row per question: the scenario, the tools the model called, whether it passed and why not."""
    rows = [("scenario", "lang", "tools called", "time", "result", "note")]
    for result in results:
        called = ", ".join(call.tool + ("" if call.outcome == "ok" else f" ({call.outcome})") for call in result.calls)
        note = "; ".join(note for note in (*result.verdict.notes, result.problem) if note)
        verdict = "ok" if result.verdict.passed else "FAIL"
        rows.append((result.scenario.name, result.language, called or "-", f"{result.seconds:.0f} s", verdict, note))
    return markdown_table(rows)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask a real model the usual questions about the demo database.")
    parser.add_argument("--model", help="model in Ollama (default: the first chat model it lists)")
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"OpenAI-compatible endpoint (default: {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--qwen", default=shutil.which("qwen"), help="path to the qwen executable (default: the one on PATH)"
    )
    parser.add_argument(
        "--lang",
        choices=(*LANGUAGES, "all"),
        default="all",
        help="the language of the questions and of the server (default: both, one after the other)",
    )
    parser.add_argument(
        "--only", help="comma-separated names of the scenarios to run: " + ", ".join(s.name for s in SCENARIOS)
    )
    parser.add_argument("--database", default=demo.DATABASE, help=f"the demo database (default: {demo.DATABASE})")
    parser.add_argument("--role", default=demo.ROLE, help=f"the agent's role in it (default: {demo.ROLE})")
    parser.add_argument(
        "--password", default=demo.PASSWORD, help="the role's password (default: the one demo.py gives)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print every answer in full")
    return parser.parse_args(argv)


def evaluate(args: argparse.Namespace, scenarios: Sequence[Scenario], model: str) -> list[Result]:
    """Register the server in a throwaway home and put every question in every language."""
    languages = LANGUAGES if args.lang == "all" else (args.lang,)
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="pg-mcp-agent-eval-") as folder:
        home = Path(folder)
        env = home_env(home, dict(os.environ), args, model)
        version = plain(run([args.qwen, "--version"], env, home).stdout).strip()
        print(f"model {model} at {args.base_url}; Qwen Code {version} ({args.qwen}); {args.database} as {args.role}")
        for language in languages:
            register(env, home, language)
            if not results:
                link_extension(args.qwen, env, home)
            for scenario in scenarios:
                result = ask(scenario, language, args.qwen, env, home)
                report(result, verbose=args.verbose)
                results.append(result)
                if len(results) == 1:
                    warn_small_context(args.base_url, model)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        scenarios = selected(args.only)
        if not args.qwen:
            raise CheckError("Qwen Code not found: npm install -g @qwen-code/qwen-code, or pass --qwen")
        model = choose_model(args.base_url, args.model)
        results = evaluate(args, scenarios, model)
    except CheckError as exc:
        print(f"cannot run the check: {exc}")
        return 2
    passed = sum(result.verdict.passed for result in results)
    print()
    print(table(results))
    print(f"\nscore: {passed}/{len(results)} passed with {model}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
