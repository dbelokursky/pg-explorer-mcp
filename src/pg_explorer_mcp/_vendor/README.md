# Зависимости в репозитории

Чистые Python-пакеты, распакованные из wheel-файлов с PyPI, чтобы сервер работал без pip и сети.
У каждого пакета сохранена папка `*.dist-info` с лицензией, а `requirements.txt` перечисляет их точные
версии, как `vendor.txt` у pip.

| Пакет | Версия | Лицензия | Зачем |
|---|---|---|---|
| pg8000 | 1.31.5 | BSD-3-Clause | драйвер PostgreSQL |
| scramp | 1.4.6 | MIT-0 | SCRAM-аутентификация для pg8000; последняя версия с поддержкой Python 3.9 |
| asn1crypto | 1.5.1 | MIT | нужен scramp |
| python-dateutil | 2.9.0.post0 | Apache-2.0 / BSD-3-Clause | разбор дат в pg8000 |
| six | 1.17.0 | MIT | нужен python-dateutil |

SHA-256 исходных wheel-файлов:

```
0af2c1926b153307639868d2ee5cef6cd3a7d07448e12736989b10e1d491e201  pg8000-1.31.5-py3-none-any.whl
a0cf9d2b4624b69bac5432dd69fecfc55a542384fe73c3a23ed9b138cda484e1  scramp-1.4.6-py3-none-any.whl
db4e40728b728508912cbb3d44f19ce188f218e9eba635821bb4b68564f8fd67  asn1crypto-1.5.1-py2.py3-none-any.whl
a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427  python_dateutil-2.9.0.post0-py2.py3-none-any.whl
4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274  six-1.17.0-py2.py3-none-any.whl
```

## Уязвимости

`requirements.txt` — манифест pip для графа зависимостей GitHub: по нему GitHub знает, какие версии
вшиты, и Dependabot присылает алерт, когда в одной из них находят уязвимость. Dependabot смотрит на
ветку по умолчанию, `main`: манифест из `develop` он увидит после выпуска, и алерт закроется, когда
исправленная версия попадёт в выпуск. Манифесты в папках вроде `vendor/` и `third_party/` GitHub не читает; `_vendor/` под его
правила не попадает, и `test_vendor.py` за этим следит.

Сами пакеты Dependabot не обновляет: записи `pip` в `.github/dependabot.yml` нет, потому что его PR поменял
бы только манифест и упал бы на `check_vendor.py`.

## Как обновить

1. Впишите нужные версии в `requirements.txt` и скачайте по нему wheel-файлы в пустую папку:

   ```bash
   python3 -m pip download --no-deps --only-binary :all: -d /tmp/pg-mcp-wheels \
     -r src/pg_explorer_mcp/_vendor/requirements.txt
   ```

2. Удалите старые пакеты из `src/pg_explorer_mcp/_vendor/` (кроме этого README и `requirements.txt`) и
   распакуйте новые:

   ```bash
   for w in /tmp/pg-mcp-wheels/*.whl; do python3 -I -m zipfile -e "$w" src/pg_explorer_mcp/_vendor/; done
   ```

3. Обновите таблицу и хеши выше и прогоните все тесты, включая интеграционные, на Python 3.9.
   Тест `test_vendor.py` (и `python3 scripts/check_vendor.py`) сверяет каждый файл с хешем из
   `RECORD` его wheel, а `requirements.txt` — с версиями в `*.dist-info`: ручная правка в
   `src/pg_explorer_mcp/_vendor/` или версия, не вписанная в манифест, его уронит.

Пока поддерживаем Python 3.9, scramp остаётся на 1.4.6: следующие версии требуют 3.10.
