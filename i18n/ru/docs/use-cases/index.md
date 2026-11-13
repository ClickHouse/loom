<!-- i18n: source=docs/use-cases/index.md blob=178effa61120f8d47e67f6ff291322133ca3e84f model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md), which is authoritative.

<a id="use-cases"></a>

# Сценарии использования

Каждая страница отвечает на один вопрос, который люди и агенты задают, когда ищут инструмент: что Loom
даёт для этой задачи и с чего начать.

| Задача                                                            | Страница                                                   |
| ----------------------------------------------------------------- | ---------------------------------------------------------- |
| Долговременная и общая память для ИИ-агентов (MCP-сервер памяти)  | [Память агента](agent-memory.md)                           |
| Поиск по знаниям и документам для агентов (RAG)                   | [Поиск по документам](document-search.md)                  |
| Индекс кода и анализ кода для кодинг-агентов                      | [Индекс кода](code-intelligence.md)                        |
| Что индекс кода даёт для каждого языка (C++, Python, TS, Go, …)   | [Индекс кода по языкам](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md) |
| Снижение стоимости модели для кодинг-агентов                      | [Стоимость кодинг-агента](coding-agent-cost.md)            |
| Общий контекст между агентами в мультиагентном процессе           | [Мультиагентность](multi-agent.md)                         |
| Память, поиск, аудит и удаление данных для автономных агентов     | [Автономные агенты](autonomous-agents.md)                  |
| Запуски агентов, трассировки, трекер и история PR                 | [Запуски и история PR](runs-and-pr-history.md)             |
| Self-hosted память агента на ClickHouse, с Docker или Kubernetes  | [Self-hosted на ClickHouse](self-hosted-clickhouse.md)     |

В разделе [Когда Loom не подходит](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) перечислены
задачи, которые лучше решает другой инструмент.
