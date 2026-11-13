<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# Снижение стоимости модели для coding-агентов

С плагином Loom и индексом кода Claude Code отвечал на вопросы о кодовой базе ClickHouse
при стоимости модели на 6–21% ниже, чем без них, на всех трёх измеренных наборах вопросов. Экономия составила 20% на 24 вопросах с оценкой ответов (качество ответов 0.84 против
0.78), 21% на 36 концептуальных вопросах и 6% на 24 multi-hop-вопросах, где отслеживание бага
до его исправления в релизной ветке стоило одинаково в обоих случаях.

<a id="what-was-measured"></a>

## Что измерялось

В обоих вариантах запускался Claude Code (`claude-sonnet-5`) на одном и том же checkout: одна обычная сессия и одна с плагином Loom и набором инструментов `coding`. Стоимость — это полный счёт за модель по всей сессии; она не включает обращения самого Loom к моделям на стороне сервера, хостинг и индексацию. Сравнение оценивает плагин целиком и не разделяет вклад индекса кода и памяти. Результаты относятся только к протестированным нагрузкам. С Loom ответ на вопрос занимает больше времени по часам: поиск плагина при каждом запросе и обращения по MCP добавляют задержку. О том, как стоимость на один вопрос сравнивается с разбросом между запусками, написано на странице результатов.
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) содержит все наборы, протокол и артефакты, а [evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) показывает, как измерить это на вашем собственном репозитории.

О памяти, которую читает модель вместо всей истории чата, см. в разделе
[agent memory](agent-memory.md#measured).

<a id="how-the-plugin-is-designed-to-save"></a>

## Как плагин экономит ресурсы

- **Ответы из индекса вместо чтения файлов.** `code.symbol`, `code.callers` и `code.search`
  возвращают строки, которые отвечают на вопрос о структуре кода. [Индекс кода](code-intelligence.md)
- **Сохранённый контекст вместо повторного поиска.** Решения и находки, сохранённые в сессии, загружаются
  в начале следующей сессии. [Общая память](agent-memory.md)

Loom также записывает, сколько тратится на embedding и вызовы LLM по каждой операции.
[Стоимость и использование](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
