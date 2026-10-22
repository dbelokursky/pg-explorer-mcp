"""The two catalogs, how the language is chosen, and what the server says in Russian."""

from __future__ import annotations

import locale
import re
import string
from pathlib import Path

import pytest

from pg_mcp import i18n, tools
from pg_mcp.budget import fit_to_budget
from pg_mcp.db import Database
from pg_mcp.protocol import Server
from pg_mcp.sqlguard import (
    FORBIDDEN_FRAGMENTS,
    FORBIDDEN_FUNCTIONS,
    FORBIDDEN_PREFIXES,
    FORBIDDEN_WORDS,
    SqlRejectedError,
    check_read_only,
)
from pg_mcp.tools import RELATION_KINDS, Tool, ToolError, build_tools
from tests.helpers import ROOT, rpc

PLURAL_SUFFIX = re.compile(r"\.(one|few|many|other)$")
SOURCES = [*sorted((ROOT / "src" / "pg_mcp").glob("*.py")), ROOT / "install.py"]
# t("key") and plural("key", n): the texts the code asks for by name.
REQUESTED = re.compile(r"""\b(t|plural)\(\s*"([\w.]+)"\s*[,)]""")
# Any string literal shaped like a key, including keys kept in tables and conditionals.
KEY_LITERAL = re.compile(r'"([a-z_]+(?:\.[a-z_]+)+)"')


def base(key: str) -> str:
    return PLURAL_SUFFIX.sub("", key)


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def keys_named_by_data() -> set[str]:
    keys = {f"kind.{kind}" for kind in RELATION_KINDS}
    keys |= {f"sql.word.{word}" for word in FORBIDDEN_WORDS}
    keys |= {f"sql.reason.{reason}" for reason in FORBIDDEN_FUNCTIONS.values()}
    keys |= {f"sql.reason.{reason}" for _, reason in (*FORBIDDEN_PREFIXES, *FORBIDDEN_FRAGMENTS)}
    return keys


class TestCatalog:
    def test_both_languages_have_the_same_texts(self) -> None:
        assert {base(key) for key in i18n.EN} == {base(key) for key in i18n.RU}

    @pytest.mark.parametrize("language", i18n.LANGUAGES)
    def test_counted_texts_have_exactly_the_plural_forms_of_their_language(self, language: str) -> None:
        catalog = i18n.CATALOG[language]
        for counted in {base(key) for key in catalog if PLURAL_SUFFIX.search(key)}:
            forms = {key for key in catalog if base(key) == counted}
            assert forms == {f"{counted}.{form}" for form in i18n.PLURAL_FORMS[language]}, counted
            assert all("count" in placeholders(catalog[form]) for form in forms), counted

    def test_translations_fill_in_the_same_placeholders(self) -> None:
        def by_text(catalog: dict[str, str]) -> dict[str, set[str]]:
            found: dict[str, set[str]] = {}
            for key, template in catalog.items():
                found.setdefault(base(key), set()).update(placeholders(template))
            return found

        assert by_text(i18n.RU) == by_text(i18n.EN)

    def test_every_text_the_code_asks_for_exists(self) -> None:
        for path in SOURCES:
            for function, key in REQUESTED.findall(source(path)):
                for language, catalog in i18n.CATALOG.items():
                    forms = [f"{key}.{form}" for form in i18n.PLURAL_FORMS[language]] if function == "plural" else [key]
                    missing = [form for form in forms if form not in catalog]
                    assert not missing, f"{path.name}: {language} has no {missing}"

    def test_every_text_named_by_data_exists(self) -> None:
        for catalog in i18n.CATALOG.values():
            assert keys_named_by_data() <= set(catalog)

    def test_every_text_is_used(self) -> None:
        named = keys_named_by_data()
        for path in SOURCES:
            named.update(KEY_LITERAL.findall(source(path)))
        assert {base(key) for key in i18n.EN} - named == set()


class TestChoosingTheLanguage:
    @pytest.mark.parametrize(
        ("env", "system", "expected"),
        [
            ({}, None, ("en", None)),
            ({"PG_MCP_LANG": "ru"}, None, ("ru", None)),
            ({"PG_MCP_LANG": " RU "}, None, ("ru", None)),
            ({"PG_MCP_LANG": "ru_RU.UTF-8"}, None, ("ru", None)),
            ({"PG_MCP_LANG": "en-US", "LANG": "ru_RU.UTF-8"}, None, ("en", None)),
            ({"PG_MCP_LANG": "de"}, "ru_RU", ("en", "de")),
            ({"LANG": "ru_RU.UTF-8"}, None, ("ru", None)),
            ({"LANG": "en_RU.UTF-8"}, None, ("en", None)),
            ({"LC_ALL": "C.UTF-8", "LANG": "ru_RU.UTF-8"}, None, ("en", None)),
            ({"LANGUAGE": "ru:en", "LC_ALL": "en_US.UTF-8"}, None, ("ru", None)),
            ({"LANG": " "}, "Russian_Russia", ("ru", None)),
            ({}, "English_United States", ("en", None)),
        ],
        ids=[
            "nothing-set",
            "explicit",
            "explicit-any-case",
            "explicit-as-a-locale",
            "explicit-beats-the-locale",
            "explicit-but-unknown",
            "locale",
            "english-in-a-russian-region",
            "lc-all-beats-lang",
            "language-beats-lc-all",
            "windows-without-lang",
            "windows-english",
        ],
    )
    def test_pg_mcp_lang_then_the_locale_variables_then_the_system(
        self, env: dict[str, str], system: str | None, expected: tuple[str, str | None]
    ) -> None:
        assert i18n.language_from_env(env, system) == expected

    def test_system_locale_is_what_python_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(locale, "getlocale", lambda: ("ru_RU", "UTF-8"))
        assert i18n.system_locale() == "ru_RU"

    def test_a_locale_python_cannot_parse_counts_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unknown() -> tuple[str | None, str | None]:
            raise ValueError("unknown locale: UTF-8")

        monkeypatch.setattr(locale, "getlocale", unknown)
        assert i18n.system_locale() is None

    def test_speaking_switches_for_one_block(self) -> None:
        with i18n.speaking("ru"):
            assert i18n.current() == "ru"
        assert i18n.current() == "en"

    def test_unknown_languages_are_refused(self) -> None:
        with pytest.raises(ValueError, match="'de'"):
            i18n.use("de")
        with pytest.raises(ValueError, match="'de'"), i18n.speaking("de"):
            pass


class TestNumbers:
    @pytest.mark.parametrize(
        ("count", "en", "ru"),
        [
            (0, "(0 rows)", "(0 строк)"),
            (1, "(1 row)", "(1 строка)"),
            (2, "(2 rows)", "(2 строки)"),
            (4, "(4 rows)", "(4 строки)"),
            (5, "(5 rows)", "(5 строк)"),
            (11, "(11 rows)", "(11 строк)"),
            (12, "(12 rows)", "(12 строк)"),
            (14, "(14 rows)", "(14 строк)"),
            (21, "(21 rows)", "(21 строка)"),
            (22, "(22 rows)", "(22 строки)"),
            (25, "(25 rows)", "(25 строк)"),
            (101, "(101 rows)", "(101 строка)"),
            (111, "(111 rows)", "(111 строк)"),
            (112, "(112 rows)", "(112 строк)"),
        ],
    )
    def test_plural_forms(self, count: int, en: str, ru: str) -> None:
        assert i18n.plural("rows.count", count) == en
        with i18n.speaking("ru"):
            assert i18n.plural("rows.count", count) == ru

    @pytest.mark.parametrize(("value", "en", "ru"), [(1.2, "1.2", "1,2"), (3.0, "3", "3"), (12.34, "12.3", "12,3")])
    def test_decimals_are_written_the_local_way(self, value: float, en: str, ru: str) -> None:
        assert i18n.number(value) == en
        with i18n.speaking("ru"):
            assert i18n.number(value) == ru

    @pytest.mark.parametrize(
        ("rows", "en", "ru"),
        [
            (-1, "rows unknown (never analyzed)", "число строк неизвестно (статистику ещё не собирали)"),
            (None, "empty or never analyzed", "пусто или статистику ещё не собирали"),
            (0, "~0 rows", "~0 строк"),
            (1, "~1 row", "~1 строка"),
            (3, "~3 rows", "~3 строки"),
            (11, "~11 rows", "~11 строк"),
            (1234, "~1.2k rows", "~1,2 тыс. строк"),
            (5_000_000, "~5M rows", "~5 млн строк"),
            (2_345_678_901, "~2.3B rows", "~2,3 млрд строк"),
        ],
    )
    def test_row_estimates(self, rows: int | None, en: str, ru: str) -> None:
        assert tools._estimate(rows) == en
        with i18n.speaking("ru"):
            assert tools._estimate(rows) == ru

    @pytest.mark.parametrize(
        ("shown", "expected"),
        [(1, "(показана 1 строка, есть"), (3, "(показаны 3 строки, есть"), (50, "(показано 50 строк, есть")],
    )
    @pytest.mark.usefixtures("russian")
    def test_a_partial_answer_agrees_with_its_count(self, shown: int, expected: str) -> None:
        lines = tools._format_rows(["n"], [(n,) for n in range(shown)], more=True)
        assert lines[-1].startswith(expected)


@pytest.mark.usefixtures("russian")
class TestRussian:
    @pytest.fixture
    def offered(self, unreachable_env: dict[str, str]) -> dict[str, Tool]:
        return {tool.name: tool for tool in build_tools(Database(unreachable_env), str(ROOT), profile="analyst")}

    def test_tools_are_described_in_russian(self, offered: dict[str, Tool]) -> None:
        assert offered["status"].title == "Состояние подключения к базе"
        assert offered["status"].describe()["annotations"]["title"] == "Состояние подключения к базе"
        assert "Возвращает не больше 200 строк" in offered["run_select"].description
        assert "реальное время" in offered["explain_query"].description
        limit = offered["sample_rows"].properties["limit"]
        assert limit["description"] == "Сколько строк, от 1 до 50. По умолчанию 10."

    def test_status_says_what_is_wrong_in_russian(self, offered: dict[str, Tool]) -> None:
        text = offered["status"].call({})
        assert "профиль: analyst (" in text
        assert "язык: ru" in text
        assert "база: недоступна. По адресу 127.0.0.1:" in text
        assert "никто не принимает подключения" in text

    def test_bad_arguments_are_explained_in_russian(self, offered: dict[str, Tool]) -> None:
        allowed = r"Допустимые: columns, limit, schema, table\."
        with pytest.raises(ToolError, match=rf"Неизвестные аргументы: rows\. {allowed}"):
            offered["sample_rows"].call({"table": "orders", "rows": 5})
        with pytest.raises(ToolError, match="Параметр table должен быть непустой строкой"):
            offered["describe_table"].call({"table": " "})

    @pytest.mark.parametrize(
        ("query", "message"),
        [
            ("DELETE FROM orders", "Команда DELETE запрещена. Разрешены только команды на чтение: SELECT, WITH"),
            (
                "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
                "Ключевое слово DELETE запрещено: DELETE меняет данные. Этот сервер только читает.",
            ),
            ("SELECT pg_sleep(1)", r"Функция pg_sleep\(\) запрещена: она держит соединение впустую\."),
            ("(SELECT 1)", "Такая команда запрещена."),
            ("SELECT 1; SELECT 2", "Отправляйте одну команду за вызов"),
        ],
    )
    def test_refusals_say_why_in_russian(self, query: str, message: str) -> None:
        with pytest.raises(SqlRejectedError, match=message):
            check_read_only(query)

    def test_bad_settings_are_explained_in_russian(self) -> None:
        assert Database({"PGPORT": "пять"}).describe_target() == "не настроено: PGPORT='пять' — не число"

    def test_a_cut_answer_says_so_in_russian(self) -> None:
        cut = fit_to_budget([f"строка {n}" for n in range(1000)], 500)
        assert len("\n".join(cut)) <= 500
        assert cut[-1].startswith("… ответ обрезан до 500 символов: показано строк — ")

    def test_the_agent_is_told_to_answer_in_the_users_language(self) -> None:
        reply = Server([]).handle(rpc(1, "initialize", {"protocolVersion": "2025-11-25"}))
        assert reply is not None
        assert "Отвечайте пользователю на том языке, на котором он пишет." in reply["result"]["instructions"]


def test_english_instructions_ask_for_the_users_language_too() -> None:
    reply = Server([]).handle(rpc(1, "initialize", {"protocolVersion": "2025-11-25"}))
    assert reply is not None
    assert "Reply to the user in the language they write in." in reply["result"]["instructions"]
