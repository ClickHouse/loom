<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# 面向 agent 的文档搜索与 RAG

Loom 会为你的文档建立索引（Markdown 和文本，以及由其提取器服务转换的 PDF、`.docx` 和 `.pptx` 文件），并让 agent 通过 MCP、HTTP 或 SDK 搜索这些文档并获得引用。它是检索增强生成（RAG）中的检索部分：由你的 agent 或模型根据 Loom 返回的段落来撰写答案。

<a id="what-you-get"></a>

## 你能得到什么

- **遵循文档结构的分块。** `knowledge.add` 会存储文本，也可以选择同时存储原始文件；Loom 按标题拆分文本，对每个章节做窗口切分，并为每个分块生成嵌入。[知识文档](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **引用。** 每条命中结果都会给出所属文档的 `source_uri` 和对应段落，因此 agent 可以引用并链接到来源。
- **一次搜索覆盖记忆、文档和对话。** `loom_search` 在一次调用中，针对调用者的各个记忆命名空间，同时运行记忆、知识、档案和对话搜索。[统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **文档来源。** 命名空间的管理员可以注册 Loom 从中拉取文档的来源；同步 worker 需要设置 `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`。[文档来源](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **相似度下限。** `loom_search` 会丢弃相似度低于下限的知识命中结果，而不是把一个相关性很弱的段落交给模型。[`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **对每个段落的访问控制。** 文档存放在命名空间中，命名空间具有 viewer、writer 和 admin 角色。[命名空间与共享](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 实测结果

在一个已提交的 PDF、DOCX 和 PPTX 文件语料库上（27 个留出文件），Loom 的提取器在开启 OCR（默认设置）的情况下，在实测运行中取得了 0.999 的文本召回率和 0.991 的表格单元格 F1（[文档解析](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)）。文档检索质量目前只有部分得到了度量；[覆盖范围与已知缺口](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)说明了还缺少哪些内容。

<a id="start"></a>

## 开始使用

[快速入门](../getting-started/quickstart.md)会连同提取器服务一起运行整个技术栈。你可以在控制台的 Library 页面添加文档；文本可使用 `knowledge.add`；对于需要提取器转换的文件，则先使用 `knowledge.upload`，再使用 `knowledge.ingest`（[导入文件](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)）。

本页中的部分 op 在[状态页](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)中被标记为实验性。
