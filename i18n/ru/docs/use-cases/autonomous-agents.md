<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# Память, recall, аудит и стирание данных для автономных агентов

Loom даёт автономным агентам память, к которой они могут обратиться позже, журнал аудита с записью о том, кто и что записал, и стирание scope везде, где Loom его хранит. Это серверные операции, данные хранятся в ClickHouse (или в Postgres для живой памяти и состояния знаний).

<a id="what-you-get"></a>

## Что вы получаете

- **Память и recall.** Версионируемые факты с указанием происхождения, поиск, который воздерживается от ответа, если ничего достаточно близкого не нашлось, и повышение в ранжировании для воспоминаний, на которые опирались успешные запуски. [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Отказ.** Закреплённый факт отклоняет противоречащие записи, а `blacklist.add` регистрирует шаблон, который не может быть сохранён ни одной записью. [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **Аудит.** Каждая операция, изменяющая память, оставляет строку аудита с SHA-256 от её содержимого, а воркер запечатывает почасовые окна в цепочку хешей, поэтому правку или удаление можно обнаружить. Записи аудита на уровне транспорта работают по принципу fail-open (неудавшаяся вставка теряет строку); при Postgres в роли authority строка аудита ревизии памяти хранится вместе с ревизией. [Audit log](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **Проверяемое стирание.** `erasure.request` стирает scope из каждой таблицы, в которой хранится содержимое памяти (по умолчанию ставятся tombstones; `purge=true` удаляет строки scope типа `user_id` или `namespace_list`), и завершается подсчётом остаточных данных и квитанцией. [Retention and erasure](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **Откат.** `namespace.rollback` возвращает каждое воспоминание в namespace к его версии на определённый момент времени. [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Headless-режим работы.** Бот получает собственную идентичность (он сам регистрируется там, где открыта саморегистрация), проверяет своё соединение перед первым запуском, и его можно диагностировать, если он замолчал. [Headless bots](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

Аудит и стирание — это инженерные примитивы, а не сертификация соответствия требованиям. [Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

Некоторые операции на этой странице помечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
