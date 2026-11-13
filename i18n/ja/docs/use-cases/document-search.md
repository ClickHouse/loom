<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# エージェント向けのドキュメント検索と RAG

Loom はドキュメント(Markdown とテキスト、および extractor サービスが変換する PDF、`.docx`、`.pptx` ファイル)をインデックス化し、エージェントが MCP、HTTP、または SDK を通じて、引用付きでそれらを検索できるようにします。これは検索拡張生成(RAG)における検索の側を担うものです。回答は、Loom が返すパッセージをもとに、エージェントまたはモデルが作成します。

<a id="what-you-get"></a>

## できること

- **ドキュメントの構造に沿ったチャンク。** `knowledge.add` はテキストを保存し、必要に応じて元のファイルも保存します。Loom は見出しでテキストを分割し、各セクションをウィンドウ分割して、すべてのチャンクを埋め込みます。[Knowledge documents](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **引用。** 検索結果にはドキュメントの `source_uri` と該当箇所が含まれるため、エージェントは出典を引用してリンクできます。
- **メモリ、ドキュメント、会話をまたぐ一括検索。** `loom_search` は、呼び出し元のメモリ namespace 全体に対して、メモリ、ナレッジ、プロファイル、会話の検索を1回の呼び出しで実行します。[Unified search](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **ドキュメントソース。** namespace の admin は、Loom がドキュメントを取得するソースを登録できます。同期ワーカーには `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED` が必要です。
  [Document sources](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **類似度の下限。** `loom_search` は、類似度が下限を下回るナレッジの検索結果を除外し、関連性の低い箇所をモデルに渡しません。[`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **すべての箇所に対するアクセス制御。** ドキュメントは namespace に属し、viewer、writer、admin のロールがあります。[Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 計測結果

コミット済みの PDF、DOCX、PPTX ファイルのコーパス(ホールドアウトした 27 ファイル)において、OCR をオン(デフォルト)にした Loom のエクストラクタは、計測した実行で テキスト再現率 0.999、表セル F1 0.991 を記録しました([document parsing](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing))。
ドキュメントに対する検索品質は、一部しか計測できていません。
まだ不足している点については、[coverage and known gaps](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) を参照してください。

<a id="start"></a>

## はじめに

[クイックスタート](../getting-started/quickstart.md)では、extractor サービスとともにスタックを起動します。ドキュメントを追加するには、ダッシュボードの Library ページを使うか、テキストの場合は `knowledge.add` を使います。extractor が変換するファイルの場合は、`knowledge.upload` を実行してから `knowledge.ingest` を実行します
([ファイルの取り込み](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files))。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と表示されています。
