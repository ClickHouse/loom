<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Запуски агентов, трассировки и история PR

Loom записывает работу, которую выполняют агенты для написания кода, в виде запусков и доступных для поиска диалогов сессий, и связывает её с историей вашего кода. Запуски содержат цели и результаты, а также стоимость использования моделей, если плагин Claude Code передаёт данные об использовании по ходам сессии (включено по умолчанию). История охватывает слитые pull request'ы, комментарии к ревью, задачи GitHub и, при подключённом источнике tracker, инциденты и запуски CI. Запись диалогов, история PR и tracker по умолчанию отключены, пока вы их не включите. После этого агент или человек может спросить, что уже пробовали раньше, что было решено и какое изменение привело к регрессии.

<a id="what-you-get"></a>

## Что вы получаете

- **Запуски.** `run.begin` и `run.complete` записывают цель задачи, её статус, память, на которую опирался
  агент, и то, что он записал. [Запуски](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Диалоги и краткие сводки сессий.** Записанные сессии доступны для поиска, а краткая сводка сессии
  позволяет следующей сессии продолжить работу. Запись выключена, пока владелец пространства имён не включит
  её командой `conversation.set_logging(enabled=true)`. [Диалоги и сессии](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Трассировка и возобновление длительных запусков агента](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **История PR.** История символа, пути или строки, краткие сводки для ревью, проверка дубликатов
  перед PR и кандидаты для bisect, как на [странице индекса кода](code-intelligence.md).
- **Записи трекера.** Проверка дубликатов задач, связи задач с PR и нестабильные тесты, читаемые напрямую
  из собственного ClickHouse команды. Требуются `LOOM_TRACKER_ORG` и зарегистрированный источник
  трекера. [Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **Трассировки и стоимость.** Loom отправляет данные OpenTelemetry и записывает собственные расходы на модели по
  каждой операции; Langfuse и ClickStack являются необязательными приёмниками.
  [Телеметрия](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Стоимость и использование](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## Результаты измерений

На публичных issues ClickHouse `tracker.duplicate_check` поставил более старый issue на первое место для 91
из 104 зафиксированных дубликатов (0.875) и назвал дубликатом 2 из 100 issues без зафиксированной связи. Результат относится к одному трекеру, а его issues (стек-трейсы, коды ошибок,
последовательно связанные дубликаты) проще, чем во многих других трекерах.
[Проверка дубликатов на публичных issues ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## Начало работы

[Подключение агента для написания кода](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) устанавливает
плагины, которые записывают сессии, когда запись включена; для [индекса кода](code-intelligence.md)
нужно добавить репозиторий и задать `LOOM_CODE_INGEST_ENABLED=true`.

Некоторые операции на этой странице помечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
