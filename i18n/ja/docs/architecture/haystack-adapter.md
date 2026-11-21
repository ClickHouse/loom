<!-- i18n: source=docs/architecture/haystack-adapter.md blob=c2cfa8a3d7cfb7c046a16c39b584aa93d1ea0420 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md), which is authoritative.

<a id="haystack-document-store-and-retriever"></a>

# Haystack ドキュメントストアとリトリーバー

オプションの Haystack 3.5 アダプターは、同期版 Loom SDK を通じて、ドキュメントストアのプロトコルとネイティブのリトリーバルコンポーネントを実装します。コア SDK とサーバーはこのフレームワークに依存しません。各 HTTP 呼び出しのクライアントの有効期間は SDK が管理します。アダプターは、サーバー、ローカルデータベース、キャッシュ、永続的なバックグラウンドプロセスを残しません。

<a id="invariants"></a>

## 不変条件

ストアは、各ネイティブドキュメント ID を、ストア固有のプレフィックスの下にあるハッシュ化されたソース URI に対応付けます。一致するタグによって、一覧取得と検索の範囲が決まります。メタデータ、埋め込み、バイナリフィールドを含むネイティブドキュメントの完全なシリアライズ結果は、ドキュメントの元ファイルのバイト列に格納されます。チャンクのメタデータには小さなフォーマットマーカーだけを持たせます。テキストはドキュメントのコンテンツとしてインデックスされ、テキストがない場合は ID がインデックスされます。すべての書き込みと削除には、Loom の通常のドキュメント書き込み、来歴、名前空間グラント、バージョン管理、消去の各経路が適用されます。ドキュメントは、ユーザー専用ではなく名前空間で共有されます。

件数の取得とフィルタリングでは、カーソルページを通じてストア全体をハイドレートします。ネイティブのフィルタヘルパーが、比較とブール論理を適用します。検索では、名前空間、タグ、URI の境界を明示して最大 100 チャンクを検索し、完全なドキュメントをハイドレートします。その後、ソース URI で重複を除去し、各ドキュメントで最も上位にランクされたヒットを保持します。これらの操作には、リクエストをまたぐスナップショットはありません。同時に書き込みが行われると、Loom からコンフリクトが返されることがあります。重複チェックと複数ドキュメントにまたがる操作は、トランザクションではありません。

シリアライズされた接続には、Haystack の環境変数 Secret 参照が含まれます。生の Secret トークンはシリアライズを拒否されます。明示的なエンドポイント URL に、シークレットを埋め込むことはできません。アダプターは Haystack のパイプラインのデシリアライズに関する信頼ポリシーを維持し、信頼するモジュールを登録することも、安全でないデシリアライズを選択することもありません。

<a id="source-entry-points"></a>

## ソースのエントリーポイント

- `sdk/python/loom/integrations/haystack.py`: `LoomDocumentStore` と `LoomRetriever`。
- `sdk/python/tests/test_haystack.py`: ネイティブのドキュメントストアとパイプラインの動作。
- `tests/integration/test_sdk_haystack.py`: 実環境でのドキュメントの永続化と検索。
