<!-- i18n: source=docs/use-cases/document-search.md blob=0848e72f516323271d09e930d51b4314d82ce625 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# エージェント向けのドキュメント検索と RAG

Loom はドキュメント（Markdown とテキスト、および extractor サービスが変換する PDF、`.docx`、`.pptx` ファイル）をインデックス化し、エージェントが MCP、HTTP、SDK を通じて引用付きで検索できるようにします。これは検索拡張生成（RAG）の検索側にあたります。回答は、Loom が返す文章をもとに、エージェントまたはモデルが書きます。

<a id="what-you-get"></a>

## 利用できる機能

- **ドキュメントの構造に沿ったチャンク。** `knowledge.add` はテキストと、必要に応じて元のファイルを保存します。Loom は見出しで分割し、各セクションをウィンドウ化して、すべてのチャンクを埋め込みます。[ナレッジドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **引用。** ヒットにはドキュメントの `source_uri` と該当箇所が含まれるため、エージェントは出典を引用してリンクできます。
- **メモリ、ドキュメント、会話をまとめた 1 回の検索。** `loom_search` は、呼び出し元のメモリ名前空間をまたいで、メモリ、ナレッジ、プロファイル、会話の検索を 1 回の呼び出しで実行します。[統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **ドキュメントソース。** 名前空間の管理者は、Loom がドキュメントを取得するソースを登録できます。同期ワーカーには `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED` が必要です。[ドキュメントソース](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **コードと並ぶドキュメント。** リポジトリのトランクのコード名前空間にあるソースから取り込まれたドキュメントには `repo:<id>` タグが付くため、そのリポジトリの検索ではコードと一緒にドキュメントを読み取ります。[ドキュメントソース](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources) · [コードにリンクされたメモリ](memory-linked-to-code.md)
- **類似度の下限。** `loom_search` は、類似度が下限を下回るナレッジのヒットを除外するため、関連性の弱い文章がモデルに渡されません。[`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **すべての文章に対するアクセス制御。** ドキュメントは、viewer、writer、admin のロールを持つ名前空間に置かれます。[名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 計測結果

コミット済みの PDF、DOCX、PPTX ファイルのコーパス（保留した 27 ファイル）では、OCR を有効にした（デフォルト）Loom の extractor は、計測した実行でテキスト再現率 0.999、表セル F1 0.991 を記録しました（[ドキュメント解析](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)）。ドキュメントに対する検索品質は一部しか計測されていません。まだ不足している点については、[カバレッジと既知のギャップ](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)を参照してください。

<a id="start"></a>

## はじめに

[クイックスタート](../getting-started/quickstart.md)では、extractor サービスを含むスタックを実行します。ドキュメントは、ダッシュボードの Library ページから、テキストなら `knowledge.add` で、extractor が変換するファイルなら `knowledge.upload` に続けて `knowledge.ingest` で追加します（[ファイルの取り込み](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)）。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と示されています。
