<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# Общий контекст для мультиагентных рабочих процессов

Loom даёт агентам в мультиагентном рабочем процессе единое место для обмена контекстом: память, которую читают все агенты в namespace, очереди задач для передачи работы от одного агента другому и журнал решений. Ваши агенты и оркестратор выполняют рабочий процесс, а Loom хранит то, что они узнают и передают друг другу.

<a id="what-you-get"></a>

## Что вы получаете

- **Общая память с указанием источника.** Каждая строка фиксирует, какой агент её записал, а при политике `auto_reinforce` по умолчанию противоречие между двумя агентами помечается для проверки, а не молча перезаписывается. [Пространства имён и общий доступ](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Политика конфликтов пространства имён](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **Очереди задач.** У каждого агента есть постоянная очередь с лизингом (leases), повторными попытками и правилами проверки; взятая в работу задача принадлежит одному владельцу в каждый момент времени. [Задачи, решения и политики](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Решения.** Что было решено в подобном случае, почему и сработало ли это.
  [Решения](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **Рабочая память.** Текст, который агент получает каждый раз, а не находит поиском, отдельный от долговременной памяти. [Рабочая память и профили](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **Контекстные пакеты.** `context.pack` собирает ограниченный по размеру набор данных, который агент загружает в начале задачи. [Единый поиск и контекстные пакеты](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Адаптеры для фреймворков.** SDK предоставляют Loom через собственные интерфейсы памяти агентных фреймворков. [Агентные фреймворки](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## Начало работы

В руководстве [Интеграция Loom в агента](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) описано, какую идентификацию отправляет каждый агент и что нужно записывать; [токены и права доступа](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) позволяют выдать каждому агенту собственный токен.

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
