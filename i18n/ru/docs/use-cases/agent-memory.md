<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Долговременная и общая память для ИИ-агентов

Loom — это self-hosted MCP-сервер и HTTP API, который даёт ИИ-агентам долговременную память с общим доступом: факт, сохранённый одним агентом в сессии, может найти следующая сессия, другой агент или человек в дашборде. На этой странице показано, как это работает и с чего начать.

<a id="what-you-get"></a>

## Что вы получаете

- **Одно утверждение на ключ в пространстве имён.** `memory.set` сохраняет факт; `memory.get` читает его
  обратно по ключу; `memory.search` и `loom_search` находят его по смыслу. Пространство имён — это
  единица совместного доступа: по умолчанию каждый участник читает все строки в нём.
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Память, которую можно исправлять.** Изменение создаёт новую версию с историей. Тот
  же факт после канонизации сливается с сохранённой строкой, тот же факт, выраженный другими словами,
  сохраняется как дубликат, противоречие помечается для проверки, а запись
  в закреплённый факт отклоняется.
  [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Поиск, который умеет воздерживаться.** Если ничто не проходит порог сходства, `memory.search`
  отвечает `abstain=true` с кодом причины и помечает каждое слабое совпадение как `below_threshold`;
  `loom_search` их отбрасывает. [Search and ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Память для разных coding-агентов.** Плагины Claude Code и Codex загружают релевантный
  контекст при старте сессии и сохраняют сессию по ходу работы; Cursor и другие клиенты MCP
  подключаются к тому же серверу. [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **Память по пользователям для вашего собственного агента.** SDK для Python и TypeScript записывают и читают
  память отдельно для каждого конечного пользователя. [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Результаты измерений

На 100 парных вопросах LongMemEval-S с одним и тем же ридером `gpt-5` ответы на основе воспоминаний, извлечённых Loom, получили 94% против 90% при использовании всей истории, при этом ридеру потребовалось на 91% меньше токенов в промпте (без учёта единовременных затрат на построение памяти); разница в точности статистически не подтверждена.
[С Loom и без него](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## Начало работы

[Quickstart](../getting-started/quickstart.md) поднимает локальный стек и сохраняет первое
воспоминание через HTTP, Python и MCP. [Когда Loom не подходит](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)
описывает случаи, в которых лучше справляется библиотека памяти, работающая внутри процесса.

Некоторые операции на этой странице помечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
