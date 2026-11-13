<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# Поиск по документам и RAG для агентов

Loom индексирует ваши документы (Markdown и текст, а также файлы PDF, `.docx` и `.pptx`, которые преобразует его сервис извлечения) и позволяет агентам искать по ним с цитированием через MCP, HTTP или SDK. Это поисковая половина генерации с дополнением выборкой (RAG): ваш агент или модель пишет ответ по фрагментам, которые возвращает Loom.

<a id="what-you-get"></a>

## Что вы получаете

- **Чанки, которые следуют структуре документа.** `knowledge.add` сохраняет текст и, при необходимости, исходный файл; Loom делит его по заголовкам, нарезает окнами каждый раздел и строит эмбеддинг для каждого чанка. [Документы базы знаний](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **Цитирование.** Результат поиска содержит `source_uri` документа и фрагмент, поэтому агент может процитировать источник и дать на него ссылку.
- **Единый поиск по памяти, документам и диалогам.** `loom_search` одним вызовом выполняет поиск по памяти, базе знаний, профилю и диалогам во всех пространствах имён памяти вызывающего. [Единый поиск](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Источники документов.** Администраторы пространства имён могут зарегистрировать источники, из которых Loom забирает документы; для рабочего процесса синхронизации нужна переменная `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`. [Источники документов](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **Порог сходства.** `loom_search` отбрасывает результаты базы знаний с сходством ниже порога, а не подсовывает модели слабый фрагмент. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **Контроль доступа к каждому фрагменту.** Документы хранятся в пространствах имён с ролями viewer, writer и admin. [Пространства имён и совместный доступ](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## Измерения

На закоммиченном корпусе файлов PDF, DOCX и PPTX (27 отложенных файлов) экстрактор Loom с включённым OCR (по умолчанию) показал полноту текста 0.999 и F1 по ячейкам таблиц 0.991 в измеренном прогоне ([разбор документов](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)). Качество поиска по документам измерено лишь частично; [покрытие и известные пробелы](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) описывают, чего ещё не хватает.

<a id="start"></a>

## Начало работы

[Краткое руководство](../getting-started/quickstart.md) запускает стек вместе со сервисом извлечения. Добавьте документ на странице Library панели управления, через `knowledge.add` для текста или, для файла, который преобразует экстрактор, через `knowledge.upload`, а затем `knowledge.ingest` ([загрузка файлов](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
