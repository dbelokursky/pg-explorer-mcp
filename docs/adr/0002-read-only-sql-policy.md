# ADR 0002. Какой SQL разрешён агенту

Статус: предложено, ждёт ревью. Нужно до инструмента `run_select` (свободный SQL).

## Принцип

Разрешено только чтение: разные `SELECT` и статистика. Любое изменение данных, структуры,
прав или настроек запрещено. Запрет держится на трёх независимых слоях: если один промахнётся,
остальные всё равно остановят запись.

| Слой | Что делает | Почему его недостаточно одного |
|---|---|---|
| Роль в PostgreSQL | только `SELECT` на нужные схемы, никаких прав на создание и изменение | это настоящая граница, но её настраивает DBA, и ошибиться легко |
| Транзакция `READ ONLY` на каждый запрос, в конце `ROLLBACK` | PostgreSQL сам отвергает запись и DDL; откат отменяет любые изменения настроек сессии | не ловит побочные эффекты функций: сигналы другим сессиям, advisory-блокировки, внешние соединения |
| Проверка текста запроса сервером | пропускает только разрешённые команды, режет запрещённые слова и функции, объясняет модели причину отказа | без настоящего парсера SQL возможны и ложные срабатывания, и обход — поэтому она третья, а не единственная |

## Разрешённые команды

Решение принимается по первому ключевому слову, после пропуска комментариев.

| Команда | Условие |
|---|---|
| `SELECT` | без `INTO` и без блокировок строк (`FOR UPDATE`, `FOR SHARE` и вариантов) |
| `WITH … SELECT` | внутри CTE только чтение: никаких `INSERT`, `UPDATE`, `DELETE`, `MERGE` |
| `VALUES`, `TABLE` | — |
| `EXPLAIN` | только для разрешённого запроса; про `ANALYZE` — вопрос 1 ниже |
| `SHOW` | любой параметр; значения, закрытые для обычных ролей, сервер скрывает сам |

## Запрещённые команды

Всё, чего нет в списке разрешённых, запрещено. Явно — чтобы модель получала понятную причину:

| Группа | Команды | Почему |
|---|---|---|
| Изменение данных | `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, `COPY` в обе стороны, `SELECT … INTO` | меняют данные или создают таблицы; `COPY … TO PROGRAM` выполняет команды на сервере |
| Структура | `CREATE`, `ALTER`, `DROP` (включая `DROP DATABASE`, `DROP SCHEMA`), `COMMENT`, `SECURITY LABEL`, `REINDEX`, `CLUSTER`, `REFRESH MATERIALIZED VIEW`, `IMPORT FOREIGN SCHEMA` | DDL |
| Права | `GRANT`, `REVOKE`, `REASSIGN OWNED`, `DROP OWNED`, `ALTER DEFAULT PRIVILEGES` | меняют доступ |
| Обслуживание | `VACUUM`, `ANALYZE`, `CHECKPOINT`, `LOAD` | пишут статистику или файлы, нагружают сервер |
| Сессия и транзакции | `SET`, `RESET`, `SET ROLE`, `SET SESSION AUTHORIZATION`, `BEGIN`, `START TRANSACTION`, `COMMIT`, `END`, `ROLLBACK`, `SAVEPOINT`, `RELEASE`, `PREPARE TRANSACTION`, `DISCARD` | могут снять режим только чтения или сменить роль; транзакцией управляет сервер |
| Код | `DO`, `CALL`, `PREPARE`, `EXECUTE`, `DEALLOCATE` | выполняют произвольный код, который проверка не видит |
| Влияние на других | `LOCK`, `LISTEN`, `NOTIFY`, `UNLISTEN` | блокируют или будят чужие сессии |
| Курсоры | `DECLARE`, `FETCH`, `MOVE`, `CLOSE` | курсором для ограничения строк управляет сервер |

## Запрещённые функции, даже внутри SELECT

Транзакция `READ ONLY` их не останавливает, поэтому они в отдельном списке:

| Группа | Функции | Почему |
|---|---|---|
| Сигналы другим сессиям | `pg_terminate_backend`, `pg_cancel_backend` | обрывают чужую работу |
| Настройки | `set_config` | меняет параметры сессии в обход запрета на `SET` |
| Advisory-блокировки | `pg_advisory_lock` и все `pg_*advisory*` | сессионные блокировки переживают `ROLLBACK` и мешают приложениям |
| Внешние соединения | `dblink`, `dblink_exec`, `dblink_connect` и вся семья `dblink*` | отдельное соединение не подчиняется нашему `READ ONLY` |
| SQL из строки | `query_to_xml`, `query_to_xmlschema`, `query_to_xml_and_xmlschema`, `cursor_to_xml`, `cursor_to_xmlschema` | выполняют текст запроса, который наша проверка не видела |
| Файлы сервера | `pg_read_file`, `pg_read_binary_file`, `pg_stat_file`, `pg_ls_*`, `lo_import`, `lo_export` | читают и пишут файлы на сервере БД |
| Администрирование | `pg_reload_conf`, `pg_rotate_logfile`, `pg_switch_wal`, `pg_create_restore_point`, `pg_promote`, `pg_*replication_slot*`, `pg_logical_emit_message`, `pg_notify` | управляют сервером и репликацией |
| Последовательности и большие объекты | `nextval`, `setval`, `lo_create`, `lo_unlink`, `lo_put`, `lowrite` | меняют состояние |
| Задержки | `pg_sleep`, `pg_sleep_for`, `pg_sleep_until` | держат соединение без пользы |

## Статистика, которую разрешаем

- Системные каталоги `pg_catalog.*` и `information_schema.*` — в пределах прав роли.
- Статистика таблиц и индексов: `pg_stat_user_tables`, `pg_stat_user_indexes`, `pg_statio_user_*`,
  `pg_stats` (сервер показывает её только по таблицам, к которым у роли есть `SELECT`).
- Размеры: `pg_relation_size`, `pg_total_relation_size`, `pg_table_size`, `pg_indexes_size`, `pg_size_pretty`.
- Оценки строк из `pg_class.reltuples`, планы через `EXPLAIN`.
- `pg_stat_activity` и `pg_stat_statements` — вопрос 2 ниже.

## Как будет выполняться `run_select`

1. Проверка текста: первое слово из разрешённых, ни одного запрещённого слова или функции вне
   строк, комментариев и идентификаторов в кавычках. Иначе — ошибка с причиной для модели.
2. `BEGIN TRANSACTION READ ONLY`.
3. Запрос одной командой через extended protocol (строку из нескольких команд PostgreSQL отвергнет)
   внутри курсора: сервер берёт не больше лимита строк плюс одну, чтобы знать, что есть ещё.
4. `ROLLBACK` — всегда, даже после успеха.
5. Ответ проходит через бюджет ответа.

## Роль для агента: черновик для DBA

```sql
CREATE ROLE pg_mcp_agent LOGIN PASSWORD '…'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5;
ALTER ROLE pg_mcp_agent SET default_transaction_read_only = on;
ALTER ROLE pg_mcp_agent SET statement_timeout = '5s';
ALTER ROLE pg_mcp_agent SET idle_in_transaction_session_timeout = '60s';

GRANT CONNECT ON DATABASE app TO pg_mcp_agent;
GRANT USAGE ON SCHEMA sales TO pg_mcp_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO pg_mcp_agent;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA sales GRANT SELECT ON TABLES TO pg_mcp_agent;
```

Чего роли не выдавать: `pg_read_all_stats` и `pg_monitor` (открывают тексты чужих запросов),
`pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_signal_backend`,
членство в ролях-владельцах таблиц. Право `CREATE` на схемах тоже не нужно: в PostgreSQL 15+
у `public` оно отозвано по умолчанию, в старых версиях это стоит проверить.

## Вопросы на ревью

1. **`EXPLAIN ANALYZE`.** Он выполняет запрос, зато показывает реальное время. Предлагаю разрешить для
   `SELECT`: работает внутри `READ ONLY` и под `statement_timeout`.
2. **`pg_stat_activity` и `pg_stat_statements`.** Роль без `pg_read_all_stats` видит тексты только своих
   запросов. Предлагаю разрешить и опереться на роль, а не запрещать на уровне сервера.
3. **Лимит строк `run_select`.** Предлагаю 200 строк; сверху его дополнительно режет бюджет ответа.
4. **Кому `run_select`.** Предлагаю включать его только профилем группы, а остальным оставить
   `describe_table`, `sample_rows` и `explain_query`.
5. **Ложные срабатывания.** `INSERT`, `UPDATE`, `DELETE` в PostgreSQL — не зарезервированные слова,
   и колонку можно назвать `update`. Такой запрос проверка отвергнет, пока имя не возьмут в кавычки.
   Предлагаю с этим смириться: случай редкий, а ошибка объяснит, что делать.
