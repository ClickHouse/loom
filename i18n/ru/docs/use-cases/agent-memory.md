<!-- i18n: source=docs/use-cases/agent-memory.md blob=ef7ede85eb9ef02954cc9bdd80508b69b4b006f0 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Долговременная и общая память для ИИ-агентов

Loom — это self-hosted MCP-сервер и HTTP API, который даёт ИИ-агентам общую долговременную память: факт, сохранённый одним агентом в сессии, может найти следующая сессия, другой агент или человек в панели управления. На этой странице показано, как это работает и с чего начать.

<a id="what-you-get"></a>

## Что вы получаете

- **Одно утверждение на ключ в пространстве имён.** `memory.set` сохраняет факт; `memory.get` читает его по ключу; `memory.search` и `loom_search` находят его по смыслу. Пространство имён — это единица совместного доступа: по умолчанию каждый участник читает в нём каждую строку.
  [Память](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Память, которую можно исправить.** Изменение создаёт новую версию с историей. Тот же факт после канонизации сливается с сохранённой строкой, тот же факт другими словами сохраняется как дубликат, противоречие помечается для проверки, а запись против закреплённого факта отклоняется.
  [Конфликты и отказ](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [История и происхождение](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Поиск, который умеет воздержаться.** Если ничто не проходит порог сходства, `memory.search` отвечает `abstain=true` с кодом причины и помечает каждое слабое совпадение как `below_threshold`; `loom_search` их отбрасывает. [Поиск и вопросы](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Долговременная память для вашего агента для написания кода, общая для разных агентов.** Плагины Claude Code и Codex загружают релевантный контекст при старте сессии и фиксируют сессию по ходу работы. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode, goose, Zed и другие MCP-клиенты подключаются к тому же серверу, поэтому факт, сохранённый одним агентом, находят остальные.
  [Подключение агента для написания кода](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [конфигурация для каждого клиента](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#other-editors-and-agents)
- **Память по пользователям для вашего собственного агента.** SDK для Python и TypeScript записывают и читают память отдельно для каждого конечного пользователя. [Ваш первый агент](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Измерения

На 100 парных вопросах LongMemEval-S с одним и тем же читателем `gpt-5` ответы по извлечённым из Loom воспоминаниям набрали 94% против 90% по всей истории, при этом токенов в промпте читателя на 91% меньше (без учёта разовых затрат на построение памяти); разница в точности статистически не подтверждена.
[С Loom и без Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## Начало работы

[Быстрый старт](../getting-started/quickstart.md) поднимает локальный стек и сохраняет первую память через HTTP, Python и MCP. [Когда Loom не подходит](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) описывает случаи, в которых лучше подходит библиотека памяти внутри процесса.

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
