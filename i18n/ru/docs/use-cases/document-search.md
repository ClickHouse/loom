<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# Поиск по документам и RAG для агентов

Loom индексирует ваши документы (Markdown и текст, а также файлы PDF, `.docx` и `.pptx`, которые преобразует его сервис извлечения) и позволяет агентам искать по ним с цитатами через MCP, HTTP или SDK. Это поисковая половина генерации с дополнением выборкой (RAG): ответ пишет ваш агент или модель на основе фрагментов, которые возвращает Loom.

<a id="what-you-get"></a>

## Что вы получаете

- **Чанки, следующие структуре документа.** `knowledge.add` сохраняет текст и,
  при необходимости, исходный файл; Loom разбивает его по заголовкам, нарезает каждый раздел на окна и
  строит эмбеддинги для каждого чанка. [Knowledge documents](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **Цитирование.** Каждый результат содержит `source_uri` документа и фрагмент текста, поэтому агент может цитировать
  источник и ссылаться на него.
- **Единый поиск по памяти, документам и разговорам.** `loom_search` выполняет поиск по памяти,
  знаниям, профилю и разговорам одним вызовом, по всем пространствам имён памяти вызывающего.
  [Unified search](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Источники документов.** Администраторы пространства имён могут зарегистрировать источники, из которых Loom загружает документы;
  для воркера синхронизации нужна переменная `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`.
  [Document sources](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **Порог сходства.** `loom_search` отбрасывает результаты из знаний, не достигающие порога сходства,
  вместо того чтобы отдавать модели слабый фрагмент. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **Контроль доступа к каждому фрагменту.** Документы хранятся в пространствах имён с ролями viewer, writer и
  admin. [Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## Результаты измерений

На зафиксированном корпусе файлов PDF, DOCX и PPTX (27 отложенных файлов) экстрактор Loom
с включённым OCR (по умолчанию) показал 0.999 по полноте текста (text recall) и 0.991 по F1 для ячеек таблиц (table cell F1) в
измеренном прогоне ([разбор документов](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)).
Качество поиска по документам измерено лишь частично;
в разделе [покрытие и известные пробелы](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) описано, чего пока не хватает.

<a id="start"></a>

## Начало работы

[Краткое руководство](../getting-started/quickstart.md) запускает стек вместе со службой extractor. Добавьте документ на странице Library панели управления, с помощью `knowledge.add` для текста или, для файла, который extractor преобразует, с помощью `knowledge.upload`, а затем `knowledge.ingest` ([загрузка файлов](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

Некоторые операции на этой странице отмечены как экспериментальные на [странице статуса](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
