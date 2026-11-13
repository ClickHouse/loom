<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# エージェント向けのドキュメント検索と RAG

Loom はドキュメント（Markdown とテキスト、および抽出サービスが変換する PDF、`.docx`、`.pptx` ファイル）をインデックス化し、エージェントが MCP、HTTP、または SDK を通じて引用付きで検索できるようにします。これは検索拡張生成（RAG）の検索を担う部分です。エージェントまたはモデルは、Loom が返すパッセージをもとに回答を作成します。

<a id="what-you-get"></a>

## 得られるもの

- **ドキュメントの構造に沿ったチャンク。** `knowledge.add` はテキストと、必要に応じて元のファイルを保存します。Loom は見出しで分割し、各セクションをウィンドウ化して、すべてのチャンクを埋め込みます。[Knowledge documents](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **引用。** ヒットには、ドキュメントの `source_uri` と該当する箇所が示されるため、エージェントは出典を引用してリンクできます。
- **メモリ、ドキュメント、会話をまたぐ1回の検索。** `loom_search` は、メモリ、ナレッジ、プロファイル、会話の各検索を1回の呼び出しで実行し、呼び出し元のメモリ名前空間をすべて対象にします。[Unified search](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **ドキュメントソース。** 名前空間の管理者は、Loom がドキュメントを取得するソースを登録できます。同期ワーカーには `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED` が必要です。[Document sources](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **類似度の下限。** `loom_search` は、類似度が下限を下回るナレッジのヒットを除外し、関連性の低い箇所をモデルに渡さないようにします。[`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **すべての箇所に対するアクセス制御。** ドキュメントは、viewer、writer、admin のロールを持つ名前空間に置かれます。[Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 計測結果

PDF、DOCX、PPTX ファイルからなるコミット済みのコーパス（ホールドアウトした 27 ファイル）に対して、Loom の抽出器を OCR オン（デフォルト）で実行した計測では、テキスト再現率 0.999、表セル F1 0.991 を記録しました（[document parsing](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)）。ドキュメントに対する検索品質は一部しか計測されていません。まだ不足している点については、[coverage and known gaps](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) を参照してください。

<a id="start"></a>

## 開始

[クイックスタート](../getting-started/quickstart.md)では、extractor サービスとともにスタックを起動します。ドキュメントを追加するには、ダッシュボードの Library ページを使うか、テキストの場合は `knowledge.add` を使います。extractor が変換するファイルの場合は、`knowledge.upload` の後に `knowledge.ingest` を使います（[ファイルの取り込み](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)）。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と示されています。
