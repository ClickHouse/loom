<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Запуски агентов, трассировки и история PR

Loom записывает работу, которую выполняют кодовые агенты, в виде запусков и доступных для поиска диалогов сессий и связывает её с историей вашего кода. Запуски содержат цели и результаты, а также стоимость моделей, если плагин Claude Code передаёт данные об использовании по ходам сессии (включено по умолчанию). История охватывает слитые pull request, комментарии к ревью, GitHub issues и, при наличии источника трекера, инциденты и запуски CI. Запись диалогов, история PR и трекер по умолчанию выключены, пока их не включат. После этого агент или человек может спросить, что уже пробовали, что решили и какое изменение внесло регрессию.

<a id="what-you-get"></a>

## Что вы получаете

- **Запуски.** `run.begin` и `run.complete` записывают цель задачи, статус, память, на которую опирался агент, и то, что он записал. [Запуски](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Диалоги и краткие описания сессий.** Записанные сессии доступны для поиска, а краткое описание сессии позволяет следующей сессии продолжить работу. Запись выключена, пока владелец пространства имён не включит её с помощью `conversation.set_logging(enabled=true)`. [Диалоги и сессии](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Трассировка и возобновление длинных запусков агентов](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **История PR.** История символа, пути или строки, краткие описания для ревью, проверка дубликатов перед PR и кандидаты для bisect, как на [странице индекса кода](code-intelligence.md).
- **Записи трекера.** Проверка дубликатов issues, связи issue с PR и нестабильные тесты, читаемые вживую из собственного ClickHouse команды. Требуются `LOOM_TRACKER_ORG` и зарегистрированный источник трекера. [Плоскость трекера](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **Трассировки и стоимость.** Loom отправляет данные OpenTelemetry и записывает собственные расходы на модели по каждой операции; Langfuse и ClickStack — необязательные приёмники.
  [Телеметрия](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Стоимость и использование](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## Измерения

На публичных issues ClickHouse `tracker.duplicate_check` поставил более старый issue на первое место для 91 из 104 записанных дубликатов (0.875) и назвал дубликатом 2 из 100 issues без записанной связи. Это результат одного трекера, и его issues (стек-трейсы, коды ошибок, последовательно связанные дубликаты) проще, чем во многих других трекерах.
[Проверка дубликатов на публичных issues ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## Начало работы

[Подключение кодового агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) устанавливает плагины, которые захватывают сессии после включения записи; для [индекса кода](code-intelligence.md) нужно добавить репозиторий и задать `LOOM_CODE_INGEST_ENABLED=true`.

Некоторые операции на этой странице помечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
