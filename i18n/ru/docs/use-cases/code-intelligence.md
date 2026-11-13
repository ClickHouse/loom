<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Индекс кода для кодинг-агентов

Loom предоставляет кодинг-агентам общий индекс ваших git-репозиториев, доступный через MCP, так что агент задаёт вопрос о структуре кода и получает строки, которые на него отвечают, вместо того чтобы читать файлы целиком. Требуется добавленный репозиторий и `LOOM_CODE_INGEST_ENABLED=true`. О том, как индекс строится и поддерживается в актуальном состоянии, читайте в [обзоре индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md).

<a id="what-you-get"></a>

## Что вы получаете

- **Определения, вызывающий код и влияние изменений.** `code.symbol`, `code.callers` и `code.impact`
  отвечают с указанием файла и строки, и каждый ответ называет коммит и уровень индекса,
  на которых он основан.
  [Операции с кодом](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Поиск по смыслу и по точной строке.** `code.search` находит, где реализован механизм;
  `code.grep` находит точное сообщение или идентификатор.
- **История и pull request'ы.** История смерженных PR с комментариями ревью, проверка на дубликаты перед созданием PR и кандидаты для bisect при регрессии. Для этого нужно включить историю PR
  (`LOOM_CODE_PR_HISTORY_ENABLED`); для комментариев ревью и открытых PR также нужен токен GitHub.
  В разделе [История, PR и релизы](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) описано, что требуется для каждой возможности.
- **Заметная актуальность.** Индекс отстаёт от ветки на смерженные коммиты, и устаревший
  ответ сообщает об этом. [Актуальность](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Плагины.** Плагин Claude Code подключается с набором инструментов `coding`, а плагин Codex —
  с `core,coding,tracker,tasks`; оба показывают инструменты для работы с кодом. Cursor и другие клиенты MCP используют
  `?toolset=coding`.
  [Подключение агента для кода](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## Результаты измерений

На репозитории ClickHouse Claude Code с Loom назвал нужный файл среди трёх предложенных
для 96.4% из 36 концептуальных вопросов (10 парных запусков) против 91.4% без него, при
стоимости модели ниже на 21% (счёт за модель агента; собственные вызовы модели в Loom не учитываются).
На 200 задачах SWE-bench Verified `code.search` ставил изменённый файл в первые 10
результатов для 82% задач (это локализация файла, а не решение задачи), против 57% у BM25 по всем
проиндексированным файлам и 79% у BM25 только по файлам `.py`; разница с вариантом «только `.py`» находится в пределах погрешности.
[С Loom и без него](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Локализация файлов в SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## Начало работы

[Добавление репозиториев](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) включает индекс для
репозитория; [обзор индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) объясняет
уровни.

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
