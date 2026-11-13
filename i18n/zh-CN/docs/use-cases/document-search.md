<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# 面向 agent 的文档搜索与 RAG

Loom 会为你的文档建立索引（Markdown 和文本文件，以及由其提取器服务转换的 PDF、`.docx` 和 `.pptx` 文件），并允许 agent 通过 MCP、HTTP 或 SDK 搜索这些文档，同时返回引用来源。它是检索增强生成（RAG）中负责检索的那一半：由你的 agent 或模型根据 Loom 返回的段落来撰写答案。

<a id="what-you-get"></a>

## 你将获得什么

- **遵循文档结构的分块。** `knowledge.add` 会存储文本，也可以选择同时存储原始文件；Loom 按标题拆分文本，对每个章节划分窗口，并为每个分块生成向量嵌入。[Knowledge documents](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **引用来源。** 每条命中结果都会给出所属文档的 `source_uri` 和对应段落，因此 agent 可以引用并链接到来源。
- **一次搜索覆盖记忆、文档和对话。** `loom_search` 在一次调用中，跨调用者的记忆命名空间，执行记忆、知识、画像和对话搜索。[Unified search](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **文档源。** 命名空间的管理员可以注册 Loom 从中拉取文档的来源；同步 worker 需要设置 `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`。
  [Document sources](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **相似度下限。** `loom_search` 会丢弃相似度低于下限的知识命中结果，而不是把质量较差的段落交给模型。[`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **每个段落都有访问控制。** 文档存放在命名空间中，命名空间有 viewer、writer 和 admin 角色。[Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 实测结果

在一组已提交的 PDF、DOCX 和 PPTX 文件语料上（27 个留出文件），Loom 的提取器在开启 OCR（默认设置）的情况下，于实测运行中取得了 0.999 的文本召回率和 0.991 的表格单元格 F1（[文档解析](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)）。
文档检索质量目前只完成了部分评测；
[覆盖范围与已知缺口](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)说明了仍然缺失的内容。

<a id="start"></a>

## 开始

[快速入门](../getting-started/quickstart.md)会连同 extractor 服务一起运行整个技术栈。你可以在控制台的 Library 页面添加文档；文本可使用 `knowledge.add`；如果是需要 extractor 转换的文件，则先使用 `knowledge.upload`，再使用 `knowledge.ingest`（参见[文件导入](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)）。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)上被标记为实验性。
