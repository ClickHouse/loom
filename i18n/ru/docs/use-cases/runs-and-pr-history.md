<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Запуски агентов, трассы и история PR

Loom записывает работу, которую выполняют кодовые агенты, в виде запусков и доступных для поиска разговоров сессий, и связывает её с историей вашего кода. Запуски содержат цели и результаты, а также стоимость использования моделей, если плагин Claude Code передаёт данные об использовании за ходы сессии (включено по умолчанию). История охватывает слитые pull request'ы, комментарии к ревью, GitHub issues и, при наличии источника tracker, инциденты и запуски CI. Запись разговоров, история PR и tracker по умолчанию выключены, пока вы их не включите. После этого агент или человек может спросить, что уже пробовали раньше, какие решения были приняты и какое изменение привело к регрессии.

<a id="what-you-get"></a>

## Что вы получаете

- **Runs.** `run.begin` и `run.complete` записывают цель задачи, её статус, воспоминания, на которые опирался агент, и то, что он записал. [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Conversations и session briefs.** Записанные сессии доступны для поиска, а session brief позволяет следующей сессии продолжить работу. Запись выключена, пока владелец namespace не включит её командой `conversation.set_logging(enabled=true)`. [Conversations and sessions](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Trace and resume long agent runs](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **История PR.** История символа, пути или строки, review briefs, проверка дубликатов перед PR и кандидаты для bisect, как на [странице индекса кода](code-intelligence.md).
- **Записи трекера.** Проверка дубликатов задач, связи задач с PR и нестабильные тесты читаются напрямую из собственного ClickHouse команды. Требуются `LOOM_TRACKER_ORG` и зарегистрированный источник трекера. [Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **Трейсы и стоимость.** Loom отправляет данные OpenTelemetry и записывает собственные расходы на модели по каждой операции; Langfuse и ClickStack являются необязательными получателями.
  [Telemetry](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Cost and usage](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## Результаты измерений

На публичных issues ClickHouse `tracker.duplicate_check` поставил более старый issue на первое место для 91
из 104 зафиксированных дубликатов (0.875) и признал дубликатом 2 из 100 issues, для которых связь не была зафиксирована. Результат получен на одном трекере, а его issues (стек-трейсы, коды ошибок,
последовательно связанные дубликаты) проще, чем во многих других трекерах.
[Проверка дубликатов на публичных issues ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## Начало работы

[Подключение кодингового агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) устанавливает
плагины, которые записывают сессии, когда запись включена; для [индекса кода](code-intelligence.md)
нужно добавить репозиторий и задать `LOOM_CODE_INGEST_ENABLED=true`.

Некоторые операции на этой странице помечены как экспериментальные на [странице статусов](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
