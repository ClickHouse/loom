<!-- i18n: source=docs/use-cases/code-intelligence.md blob=4f52e242dcb612150541843af58b15f37c31a265 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Индекс кода для кодинг-агентов

Loom предоставляет кодинг-агентам общий индекс ваших git-репозиториев через MCP, так что агент задаёт вопрос о структуре кода и получает строки, которые на него отвечают, вместо того чтобы читать файлы целиком. Требуется добавленный репозиторий и `LOOM_CODE_INGEST_ENABLED=true`. Как индекс строится и поддерживается актуальным, описано в [обзоре индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md);
что он даёт для каждого языка, описано в [индексе кода по языкам](code-intelligence-by-language.md).

<a id="what-you-get"></a>

## Что вы получаете

- **Определения, вызывающий код и влияние изменений.** `code.symbol`, `code.callers` и `code.impact`
  отвечают файлом и строкой, и в каждом ответе указаны коммит и уровень индекса, на которых он основан.
  [Операции с кодом](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Поиск по смыслу и по точной строке.** `code.search` находит, где реализован механизм;
  `code.grep` находит точное сообщение или идентификатор.
- **История и pull request-ы.** История смерженных PR с комментариями ревью, проверка дубликатов перед созданием PR и кандидаты для bisect при регрессии. Для этого нужно включить историю PR
  (`LOOM_CODE_PR_HISTORY_ENABLED`); для комментариев ревью и открытых PR также нужен токен GitHub.
  В разделе [История, PR и релизы](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) указано, что требуется для каждой возможности.
- **Заметная актуальность.** Индекс отстаёт от ветки на смерженные коммиты, и устаревший ответ сообщает об этом. [Актуальность](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Плагины.** Плагин Claude Code подключается через набор инструментов `coding`, а плагин Codex —
  через `core,coding,tracker,tasks`; оба показывают инструменты для работы с кодом. Cursor и другие MCP-клиенты используют
  `?toolset=coding`.
  [Подключение кодинг-агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## Измерения

На репозитории ClickHouse Claude Code просили завершать каждый ответ списком из не более чем трёх файлов. С Loom он назвал правильный файл в 96.4% из 36 концептуальных вопросов (10 парных запусков) против 91.4% без Loom, а запуски с Loom обошлись на 21% дешевле (учитывается расход на модель агента; собственные вызовы модели в Loom не учитываются).
На 200 задачах SWE-bench Verified `code.search` поставил изменённый файл в первые 10 результатов в 82% случаев (локализация файла, а не решение задачи), против 57% у BM25 по всем проиндексированным файлам и 79% у BM25 только по файлам `.py`; разрыв с вариантом `.py` находится в пределах шума.
[С Loom и без Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Локализация файлов в SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## Начало работы

[Добавление репозиториев](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) включает индекс для репозитория; [обзор индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) объясняет уровни.

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
