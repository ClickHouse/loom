<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# Меньшая стоимость модели для агентов программирования

С плагином Loom и индексом кода Claude Code отвечал на вопросы о кодовой базе ClickHouse
при стоимости модели на 6–21% ниже, чем без него, на всех трёх измеренных наборах вопросов. Экономия составила 20% на 24 вопросах с оценкой ответов (качество ответов 0.84 против
0.78), 21% на 36 концептуальных вопросах и 6% на 24 многошаговых вопросах, где отслеживание бага
до его исправления в релизной ветке стоило одинаково в обоих случаях.

<a id="what-was-measured"></a>

## Что измерялось

В обоих вариантах запускался Claude Code (`claude-sonnet-5`) на одной и той же копии репозитория: одна обычная сессия, другая
с плагином Loom и набором инструментов `coding`. Стоимость — это весь счёт за модель в рамках сессии; она
не включает собственные серверные вызовы моделей Loom, хостинг и индексацию. Сравнение оценивает
плагин целиком; индекс кода и память оно не разделяет. Результаты
относятся только к протестированным нагрузкам. С Loom вопрос занимает больше времени по часам:
поиск плагина при каждом запросе и обращения MCP добавляют задержку. О том, как стоимость отдельного вопроса
сравнивается с шумом между запусками, рассказано на странице результатов.
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) содержит все наборы, протокол
и артефакты, а [evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) показывает, как
измерить это на вашем собственном репозитории.

О памяти, которую читает модель вместо всей истории чата, см.
[память агента](agent-memory.md#measured).

<a id="how-the-plugin-is-designed-to-save"></a>

## Как плагин устроен для экономии

- **Ответы из индекса вместо чтения файлов.** `code.symbol`, `code.callers` и `code.search`
  возвращают строки, которые отвечают на вопрос о структуре. [Индекс кода](code-intelligence.md)
- **Сохранённый контекст вместо повторного поиска.** Решения и находки, которые сохраняет сессия, загружаются
  в начале следующей сессии. [Общая память](agent-memory.md)

Loom также записывает, сколько он тратит на эмбеддинги и вызовы LLM по каждой операции.
[Стоимость и использование](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
