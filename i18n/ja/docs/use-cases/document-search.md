<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# エージェント向けのドキュメント検索と RAG

Loom はドキュメント（Markdown とテキスト、および extractor サービスが変換する PDF、`.docx`、`.pptx` ファイル）をインデックスし、エージェントが MCP、HTTP、SDK を通じて引用付きで検索できるようにします。これは検索拡張生成（RAG）の検索側にあたります。回答は、Loom が返す文章をもとにエージェントまたはモデルが書きます。

<a id="what-you-get"></a>

## 提供される機能

- **ドキュメントの構造に沿ったチャンク。** `knowledge.add` はテキストと、必要に応じて元のファイルを保存します。Loom は見出しで分割し、各セクションをウィンドウ化して、すべてのチャンクを埋め込みます。[Knowledge documents](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **引用。** ヒットには、そのドキュメントの `source_uri` と該当する文章が含まれるため、エージェントは出典を引用してリンクできます。
- **メモリ、ドキュメント、会話をまたぐ 1 回の検索。** `loom_search` は、メモリ、ナレッジ、プロファイル、会話の検索を、呼び出し元のメモリ名前空間をまたいで 1 回の呼び出しで実行します。[Unified search](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **ドキュメントソース。** 名前空間の admin は、Loom がドキュメントを取得するソースを登録できます。同期ワーカーには `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED` が必要です。[Document sources](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **類似度の下限。** `loom_search` は、類似度が下限を下回るナレッジのヒットを除外し、関連の薄い文章をモデルに渡さないようにします。[`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **すべての文章に対するアクセス制御。** ドキュメントは viewer、writer、admin のロールを持つ名前空間に置かれます。[Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 計測結果

コミット済みの PDF、DOCX、PPTX ファイルのコーパス（保持しておいた 27 ファイル）で、OCR を有効（デフォルト）にした Loom の extractor は、計測した実行でテキスト再現率 0.999、表セル F1 0.991 を記録しました（[document parsing](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)）。ドキュメントに対する検索品質の計測は一部にとどまっています。まだ不足している点は [coverage and known gaps](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) を参照してください。

<a id="start"></a>

## はじめる

[クイックスタート](../getting-started/quickstart.md)では、extractor サービスを含むスタックを起動します。ドキュメントの追加は、ダッシュボードの Library ページから行うか、テキストなら `knowledge.add` を使います。extractor が変換するファイルの場合は、`knowledge.upload` のあとに `knowledge.ingest` を使います（[ingesting files](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)）。

このページの一部の op は、[status ページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
