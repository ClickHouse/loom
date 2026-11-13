<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# Память, recall, аудит и стирание для автономных агентов

Loom даёт автономным агентам память, к которой они могут обратиться позже, аудиторскую запись о том, кто что записал, и стирание области данных везде, где Loom её хранит. Это серверные операции, данные хранятся в ClickHouse (или в Postgres для живой памяти и состояния знаний).

<a id="what-you-get"></a>

## Что вы получаете

- **Память и recall.** Версионируемые факты с указанием происхождения, поиск, который воздерживается от ответа, если ничего не подходит достаточно близко, и повышение в ранжировании для воспоминаний, на которые опирались успешные запуски. [Память](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Запуски](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Отказ.** Закреплённый факт отклоняет противоречащие записи, а `blacklist.add` регистрирует шаблон, который не может быть сохранён ни одной записью. [Конфликты и отказ](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **Аудит.** Каждая операция, изменяющая память, оставляет строку аудита с SHA-256 от её содержимого, а воркер запечатывает почасовые окна в цепочку хешей, поэтому правка или удаление обнаруживаются. Записи аудита на уровне транспорта работают в режиме fail-open (если вставка не удалась, строка теряется); при Postgres в качестве источника истины строка аудита ревизии памяти хранится вместе с ревизией. [Журнал аудита](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **Проверяемое стирание.** `erasure.request` стирает область данных из каждой таблицы, где хранится содержимое памяти (по умолчанию ставит tombstone; `purge=true` удаляет строки области `user_id` или `namespace_list`), и завершается подсчётом остатков и квитанцией. [Хранение и стирание](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **Откат.** `namespace.rollback` возвращает каждую память в пространстве имён к её версии на заданный момент времени. [История и происхождение](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Работа без интерфейса.** Бот получает собственную идентичность (он регистрируется сам, если открыта саморегистрация), проверяет своё соединение перед первым запуском, и его можно диагностировать, если он замолчал. [Боты без интерфейса](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

Аудит и стирание — это инженерные примитивы, а не сертификация соответствия требованиям. [Безопасность](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

Некоторые операции на этой странице помечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
