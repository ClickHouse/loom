<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Долговременная и общая память для AI-агентов

Loom — это self-hosted MCP-сервер и HTTP API, который даёт AI-агентам долговременную общую память: факт, сохранённый одним агентом в сессии, может найти следующая сессия, другой агент или человек в панели управления. На этой странице показано, как это работает и с чего начать.

<a id="what-you-get"></a>

## Что вы получаете

- **Одно утверждение на ключ в пространстве имён.** `memory.set` сохраняет факт; `memory.get` читает его по ключу; `memory.search` и `loom_search` находят его по смыслу. Пространство имён — это единица совместного доступа: по умолчанию каждый участник читает в нём все строки.
  [Память](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Память, которую можно исправлять.** Изменение создаёт новую версию с историей. Тот же факт после канонизации сливается с сохранённой строкой, тот же факт другими словами сохраняется как дубликат, противоречие помечается для проверки, а запись против закреплённого факта отклоняется.
  [Конфликты и отказ](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [История и происхождение](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Поиск, который умеет воздержаться.** Когда ничто не проходит порог сходства, `memory.search` отвечает `abstain=true` с кодом причины и помечает каждый слабый результат как `below_threshold`; `loom_search` такие результаты отбрасывает. [Поиск и ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Память для разных кодинг-агентов.** Плагины Claude Code и Codex загружают нужный контекст при старте сессии и сохраняют сессию по ходу работы; Cursor и другие MCP-клиенты подключаются к тому же серверу. [Подключение кодингового агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **Память по пользователям для вашего агента.** SDK для Python и TypeScript записывают и читают память для каждого конечного пользователя. [Ваш первый агент](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Измерения

На 100 парных вопросах LongMemEval-S с одним и тем же ридером `gpt-5` ответы по извлечённым из Loom воспоминаниям набрали 94% против 90% при ответах по всей истории, при этом токенов в промптах ридера на 91% меньше (без учёта разовых затрат на построение памяти); разница в точности статистически не подтверждена.
[С Loom и без Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## Начало работы

[Быстрый старт](../getting-started/quickstart.md) поднимает локальный стек и сохраняет первую память через HTTP, Python и MCP. [Когда Loom не подходит](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) описывает случаи, в которых лучше подойдёт библиотека памяти внутри процесса.

Некоторые операции на этой странице помечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
