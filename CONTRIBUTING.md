# Разработка pg-mcp

## Окружение

Нужен [uv](https://docs.astral.sh/uv/). Он сам поставит Python 3.9 (версия закреплена в
`.python-version`) и инструменты разработки ровно тех версий, что записаны в `uv.lock`:

```bash
uv sync
```

Сам сервер по-прежнему работает без зависимостей. Всё, что ставит `uv sync`, нужно только
для разработки и проверок.

Один раз включите хуки, чтобы проверки шли перед каждым коммитом:

```bash
uvx pre-commit install
```

## Проверки

| Команда | Что делает |
|---|---|
| `uv run pytest` | тесты; интеграционные без базы пропускаются |
| `uv run pytest --cov` | тесты с покрытием по веткам; порог 95% задан в `pyproject.toml` |
| `uv run ruff check --fix` | линтер с автоисправлениями |
| `uv run ruff format` | форматирование |
| `uv run mypy` | проверка типов в режиме strict |
| `uv run python scripts/check_vendor.py` | целостность `src/pg_mcp/_vendor` |
| `python3 scripts/client_smoke.py --qwen "$(command -v qwen)"` | проверка в настоящем клиенте, Qwen Code (`npm install -g @qwen-code/qwen-code`), по переменным `PG*`; модель не нужна |

Интеграционные тесты ходят в настоящий PostgreSQL. Они создают и удаляют схемы `mcp_it`,
`mcp_it_archive`, `mcp_it_role` и роли `mcp_it_agent`, `mcp_it_login`, а тесты стендов — схемы
`mcp_it_billing`, `mcp_it_orders` и одноимённые роли; тестам роли нужен `psql`:

```bash
PG_MCP_IT=1 PGHOST=localhost PGDATABASE=pg_mcp_test uv run pytest --cov
```

CI гоняет то же самое: линтеры и типы, тесты на Linux, Windows и macOS под Python 3.9 и 3.14,
интеграционные тесты на PostgreSQL с 13-й по 18-ю (покрытие — на 16-й), интеграционные тесты на Windows
с PostgreSQL 17 и проверку в Qwen Code.

## Проверка в GigaCode CLI

CI проверяет сервер в Qwen Code — открытой основе GigaCode CLI: `scripts/client_smoke.py` в
одноразовой домашней папке регистрирует сервер через `install.py`, ждёт от `qwen mcp list` статуса
`Connected` и подключает репозиторий как расширение. Сам GigaCode CLI вне компании не достать, поэтому
перед выпуском его проверяют вручную:

1. `python3 install.py` с переменными `PG*` тестовой базы — в конце `database: connected`.
2. `gigacode mcp list`, если эта команда есть в вашей версии, — у сервера `pg` статус `Connected`.
3. `gigacode extensions link .`, затем `gigacode extensions list` — расширение `pg-mcp` с командой
   `/pg:setup` и контекстом `pg-mcp-context.md`.
4. В GigaCode: «Проверь подключение к базе» (агент вызывает `status`), «Какие таблицы есть?»
   (`list_tables`) и просьба удалить строки — агент должен получить отказ проверки SQL.

## Выпуск

1. Ветка `release/X.Y.Z` от `develop`: новая версия в `pyproject.toml`, `src/pg_mcp/__init__.py` и
   обоих манифестах расширения, раздел «Не выпущено» в `CHANGELOG.md` становится `## X.Y.Z — дата`.
   `python3 scripts/release.py version` печатает версию, когда все файлы согласны.
2. PR в `develop`, затем PR `develop` → `main`.
3. Аннотированный тег на коммит слияния в `main`: `git tag -a vX.Y.Z -m "pg-mcp X.Y.Z"`,
   `git push origin vX.Y.Z`.

Пуш тега запускает `.github/workflows/release.yml`: он сверяет тег с версией в файлах, собирает
`pg-mcp-X.Y.Z.zip` из дерева тега (`git archive`; `.github` в архив не попадает по `export-ignore` в
`.gitattributes`), распаковывает архив и запускает из него `install.py --check` без базы
(`scripts/release.py check`), считает `SHA256SUMS` и публикует GitHub Release с заметками из раздела
этой версии в `CHANGELOG.md`. Так pg-mcp берут пользователи без git (README, шаг 1). Сборку можно
проверить заранее, ещё в PR: ручной запуск workflow в режиме «только собрать» —
`gh workflow run release.yml --ref <ветка> -f build_only=true` — делает всё то же, кроме публикации,
и оставляет архив в артефактах запуска.

## Раскладка

| Путь | Что там |
|---|---|
| `src/pg_mcp/` | пакет сервера |
| `src/pg_mcp/_vendor/` | pg8000 и его зависимости на чистом Python, как их выпустили на PyPI |
| `run.py` | запуск из клона репозитория: `python -I run.py` |
| `install.py` | регистрация сервера в настройках GigaCode или Qwen Code |
| `tests/` | тесты pytest; общие фикстуры в `conftest.py`, помощники в `helpers.py` |
| `scripts/` | служебные скрипты разработки |
| `docs/adr/` | принятые решения и их причины |

## Правила

- **Без зависимостей в рантайме.** Только стандартная библиотека Python 3.9+. Если без пакета
  не обойтись, он должен быть на чистом Python и попасть в `src/pg_mcp/_vendor/` по инструкции
  из его README.
- **Ветки и PR.** Ветки от `develop` с префиксом `feature/` или `fix/`, PR в `develop`, слияние
  только при зелёном CI.
- **Тексты на двух языках.** Всё, что читают модель и пользователь, — в `src/pg_mcp/i18n.py`,
  по ключу в каждом из словарей `EN` и `RU`; в коде — только `t("ключ")` или `plural("ключ", n)`.
  `tests/test_i18n.py` сверяет ключи, формы множественного числа и плейсхолдеры. Журнал и ошибки
  JSON-RPC остаются английскими (ADR 0004).
- **Версия в нескольких файлах.** `pyproject.toml`, `src/pg_mcp/__init__.py`, оба манифеста
  расширения и заголовок в `CHANGELOG.md`; за совпадением следит `tests/test_version.py`, а тег
  выпуска `vX.Y.Z` сверяет с ними `scripts/release.py`.
- **Почему так.** Решения про стек и инструменты описаны в [docs/adr/](docs/adr/); прежде чем
  менять принятое, допишите новый ADR.
