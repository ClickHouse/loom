<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# Поиск по документам и RAG для агентов

Loom индексирует ваши документы (Markdown и текстовые файлы, а также файлы PDF, `.docx` и `.pptx`, которые преобразует его сервис extractor) и позволяет агентам искать по ним с цитированием через MCP, HTTP или SDK. Это часть retrieval-augmented generation (RAG), отвечающая за поиск: ответ по найденным Loom фрагментам составляет ваш агент или модель.

<a id="what-you-get"></a>

## Что вы получаете

- **Чанки, следующие структуре документа.** `knowledge.add` сохраняет текст и,
  при необходимости, исходный файл; Loom разбивает его по заголовкам, нарезает каждый раздел на окна и
  создаёт эмбеддинги для каждого чанка. [Документы knowledge](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **Цитаты.** Найденный результат содержит `source_uri` документа и фрагмент текста, так что агент может процитировать
  источник и дать на него ссылку.
- **Единый поиск по памяти, документам и диалогам.** `loom_search` выполняет поиск по памяти,
  knowledge, профилю и диалогам за один вызов, охватывая пространства имён памяти вызывающего.
  [Единый поиск](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Источники документов.** Администраторы пространства имён могут зарегистрировать источники, из которых Loom забирает документы;
  для воркера синхронизации нужна переменная `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`.
  [Источники документов](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **Порог сходства.** `loom_search` отбрасывает результаты knowledge с оценкой ниже порога сходства,
  а не передаёт модели слабо релевантный фрагмент. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **Контроль доступа для каждого фрагмента.** Документы хранятся в пространствах имён с ролями viewer, writer и
  admin. [Пространства имён и совместный доступ](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## Измерения

На зафиксированном корпусе файлов PDF, DOCX и PPTX (27 отложенных файлов) экстрактор Loom с включённым OCR (это его настройка по умолчанию) показал в измеренном прогоне полноту текста 0.999 и F1 по ячейкам таблиц 0.991 ([разбор документов](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)).
Качество поиска по документам измерено лишь частично;
о том, чего всё ещё не хватает, сказано в разделе [покрытие и известные пробелы](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md).

<a id="start"></a>

## Начало работы

В [quickstart](../getting-started/quickstart.md) стек запускается вместе с сервисом extractor. Добавьте документ на странице Library в дашборде, с помощью `knowledge.add` для текста или, для файла, который extractor преобразует, с помощью `knowledge.upload`, а затем `knowledge.ingest`
([ingesting files](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
