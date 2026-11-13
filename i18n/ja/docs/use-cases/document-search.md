<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# エージェント向けのドキュメント検索と RAG

Loom はドキュメント（Markdown とテキスト、および extractor サービスが変換する PDF、`.docx`、`.pptx` ファイル）をインデックス化し、エージェントが MCP、HTTP、SDK を通じて引用付きで検索できるようにします。これは検索拡張生成（RAG）のうち検索を担う部分です。回答は、Loom が返すパッセージをもとに、エージェントまたはモデルが書きます。

<a id="what-you-get"></a>

## 提供される機能

- **ドキュメントの構造に沿ったチャンク。** `knowledge.add` はテキストと、必要に応じて元のファイルを保存します。Loom は見出しで分割し、各セクションをウィンドウ化して、すべてのチャンクを埋め込みます。[ナレッジドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **引用。** ヒットにはドキュメントの `source_uri` とパッセージが含まれるため、エージェントは出典を引用してリンクできます。
- **メモリ、ドキュメント、会話をまたぐ 1 回の検索。** `loom_search` は、呼び出し元のメモリ名前空間をまたいで、メモリ、ナレッジ、プロフィール、会話の検索を 1 回の呼び出しで実行します。[統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **ドキュメントソース。** 名前空間の管理者は、Loom がドキュメントを取得するソースを登録できます。同期ワーカーには `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED` が必要です。[ドキュメントソース](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **類似度の下限。** `loom_search` は、弱いパッセージをモデルに渡す代わりに、類似度の下限を下回るナレッジのヒットを除外します。[`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **すべてのパッセージに対するアクセス制御。** ドキュメントは、viewer、writer、admin のロールを持つ名前空間に置かれます。[名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 計測結果

PDF、DOCX、PPTX ファイルでコミット済みのコーパス（ホールドアウトした 27 ファイル）で、OCR を有効（デフォルト）にした Loom の extractor は、計測した実行でテキスト再現率 0.999、表セル F1 0.991 を記録しました（[ドキュメント解析](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)）。ドキュメントに対する検索品質は一部しか計測されていません。まだ不足している点は[カバレッジと既知のギャップ](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)を参照してください。

<a id="start"></a>

## 始める

[クイックスタート](../getting-started/quickstart.md)では、extractor サービスを含むスタックを起動します。ドキュメントは、ダッシュボードの Library ページから、テキストなら `knowledge.add` で、extractor が変換するファイルなら `knowledge.upload` の後に `knowledge.ingest` で追加します（[ファイルの取り込み](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)）。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
