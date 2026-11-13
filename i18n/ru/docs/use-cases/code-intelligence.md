<!-- i18n: source=docs/use-cases/code-intelligence.md blob=71addec3496d26800b8e03234eb0c0adc578b52c model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Индекс кода для агентов-программистов

Loom даёт агентам-программистам общий индекс ваших git-репозиториев, доступный по MCP: агент задаёт вопрос о структуре кода и получает строки, которые на него отвечают, вместо того чтобы читать файлы целиком. Нужен добавленный репозиторий и `LOOM_CODE_INGEST_ENABLED=true`. Как индекс строится и поддерживается в актуальном состоянии, описано в [обзоре индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md);
что он даёт для каждого языка, описано в [индексе кода по языкам](code-intelligence-by-language.md).

<a id="what-you-get"></a>

## Что вы получаете

- **Определения, вызывающие функции и влияние изменений.** `code.symbol`, `code.callers` и `code.impact`
  отвечают файлом и строкой, и каждый ответ указывает коммит и уровень индекса, на которых он основан.
  [Операции с кодом](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Поиск по смыслу и по точной строке.** `code.search` находит, где реализован механизм;
  `code.grep` находит точное сообщение или идентификатор.
- **История и pull request-ы.** История смерженных PR с комментариями ревью, проверка дубликатов перед созданием PR и кандидаты для bisect при регрессии. Для них нужно включить историю PR
  (`LOOM_CODE_PR_HISTORY_ENABLED`); для комментариев ревью и открытых PR также нужен токен GitHub.
  [История, PR и релизы](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) описывает, что нужно для каждой возможности.
- **Свежесть, которую видно.** Индекс отстаёт от ветки на смерженные коммиты, и устаревший ответ сообщает об этом. [Свежесть](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Плагины и MCP-клиенты.** Плагин Claude Code подключается с набором инструментов `coding`, плагин Codex — с `core,coding,tracker,tasks`; оба показывают инструменты для работы с кодом. Cursor, GitHub
  Copilot, Gemini CLI, Cline, Kilo Code, OpenCode и другие MCP-клиенты показывают их при `?toolset=coding` в URL (`loom install` добавляет его для Cursor; `--toolset coding` добавляет
  его для остальных, которые он настраивает).
  [Подключение агента-программиста](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP-клиенты](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## Измерения

На репозитории ClickHouse Claude Code просили завершать каждый ответ списком из не более чем трёх файлов. С Loom он назвал правильный файл в 96.4% из 36 концептуальных вопросов (10 парных запусков) против 91.4% без Loom, а запуски с Loom обходились на 21% дешевле (счёт за модель агента; собственные вызовы модели в Loom не учтены).
На 200 задачах SWE-bench Verified `code.search` ставил изменённый файл в первые 10 результатов в 82% случаев (локализация файла, а не решение задачи), против 57% у BM25 по всем проиндексированным файлам и 79% у BM25 только по файлам `.py`; разница с вариантом только по `.py` находится в пределах шума.
[С Loom и без Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Локализация файлов на SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## Начало работы

[Добавление репозиториев](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) включает индекс для репозитория;
[обзор индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) объясняет уровни.

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
