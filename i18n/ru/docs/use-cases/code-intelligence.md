<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Индекс кода для coding-агентов

Loom предоставляет coding-агентам общий индекс ваших git-репозиториев, доступный по MCP, так что агент задаёт вопрос о структуре кода и получает строки, которые на него отвечают, вместо того чтобы читать файлы целиком. Для работы нужно добавить репозиторий и задать `LOOM_CODE_INGEST_ENABLED=true`. О том, как индекс строится и поддерживается актуальным, рассказано в [обзоре индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md).

<a id="what-you-get"></a>

## Что вы получаете

- **Определения, вызывающие функции и влияние изменений.** `code.symbol`, `code.callers` и `code.impact`
  отвечают с указанием файла и строки, и в каждом ответе названы коммит и уровень индекса, на которых он основан.
  [Операции с кодом](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Поиск по смыслу и по точной строке.** `code.search` находит, где реализован механизм;
  `code.grep` находит точное сообщение или идентификатор.
- **История и pull request'ы.** История смерженных PR с комментариями ревью, проверка дубликатов перед созданием PR и кандидаты для bisect при регрессии. Для этого нужно включить историю PR
  (`LOOM_CODE_PR_HISTORY_ENABLED`); для комментариев ревью и открытых PR также нужен токен GitHub.
  [История, PR и релизы](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) описывает, что требуется для каждой возможности.
- **Заметная актуальность.** Индекс отстаёт от ветки на смерженные коммиты, и устаревший ответ сообщает об этом. [Актуальность](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Плагины.** Плагин Claude Code подключается через набор инструментов `coding`, а плагин Codex —
  через `core,coding,tracker,tasks`; оба показывают инструменты для работы с кодом. Cursor и другие MCP-клиенты используют
  `?toolset=coding`.
  [Подключение coding-агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## Результаты измерений

На репозитории ClickHouse Claude Code с Loom называл правильный файл среди своих трёх для
96.4% из 36 концептуальных вопросов (10 парных запусков) против 91.4% без Loom, при этом стоимость
использования модели была на 21% ниже (учитывается счёт за модель агента; собственные вызовы моделей Loom не включены).
На 200 задачах SWE-bench Verified `code.search` ставил изменённый файл в первые 10
результатов в 82% случаев (локализация файла, а не решение задачи), против 57% у BM25 по всем
проиндексированным файлам и 79% у BM25 только по файлам `.py`; разница с вариантом `.py` лежит в пределах шума.
[С Loom и без Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Локализация файла в SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## Начало работы

[Добавление репозиториев](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) включает индекс для
репозитория; [обзор индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) объясняет
уровни.

Некоторые операции на этой странице помечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
