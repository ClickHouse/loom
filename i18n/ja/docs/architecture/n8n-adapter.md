<!-- i18n: source=docs/architecture/n8n-adapter.md blob=f2efbe08cacbe3e9b1bb2cb39161b71c20e2be32 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md), which is authoritative.

<a id="n8n-adapter"></a>

# n8n アダプター

n8n コミュニティパッケージは、TypeScript SDK を通じて、会話履歴と永続的なファクトを Loom のデプロイメントに保存します。このノードは、ストレージサービスやローカルキャッシュを追加しません。

<a id="interfaces-and-storage"></a>

## インターフェースとストレージ

`LoomMemory.supplyData` は、フレームワークの `BufferWindowMemory` を返します。これは、`BaseListChatMessageHistory` の実装である `LoomChatHistory` を基盤としています。メッセージのシリアライズには、フレームワークの保存メッセージ形式を使用します。ウィンドウはモデルに送信するコンテキストを変更しますが、古いメッセージはその後の読み取りや明示的なクリアの対象として残ります。

各メッセージは `episodic` メモリとなり、キーにはタイムスタンプとランダムなサフィックスが含まれます。episodic の書き込みでは、セマンティックなマージを行わずに、繰り返されたメッセージを保持します。履歴のスコープは、Loom エージェントの組織、名前空間、エージェント名によって決まります。残りの識別フィールドには、ユーザーと、ワークフロー／セッション識別子のダイジェストを使用します。読み取りでは `memory.list` でページを順に取得し、射影された行をフィルターする際に重複するページを除外し、キーでソートし、切り詰められた値には `memory.get` を使用します。`clear` は、これらの項目のみを削除します。複数のライターが同時に書き込む場合、ターン全体にわたるトランザクションはありません。

Loom アクションノードは、`memory.set`、`memory.search`、`knowledge.search` を呼び出します。入力として、組織、名前空間、エージェント名、任意のユーザーを指定します。サーバーは、トークンと操作から来歴を導出し、名前空間の権限付与を強制します。

<a id="packaging"></a>

## パッケージング

このパッケージは、他のコミュニティノードとのランタイム依存関係の競合を避けるため、SDK とフレームワークのライブラリをバンドルしています。ビルドには、それらのライセンスファイルが含まれます。`n8n-workflow` は、ホストが提供するピア依存です。開発用の依存関係は、テストで使用するインターフェースを固定するもので、Loom のサーバーや SDK の依存関係には追加されません。

このパッケージの対象はセルフホストの n8n であり、そのターゲット向けに公式ノード CLI の設定を使用しています。Cloud での検証済みとは謳っていません。リリースタグのワークフローは、パッケージをビルドして検査したうえで、npm の trusted publisher と provenance を使って公開します。リポジトリにレジストリのトークンは保存されません。

<a id="invariants"></a>

## 不変条件

- アダプターは、n8n の認証情報に設定された Loom の URL にのみデータを送信します。
- アクセスを認可するのはトークンの権限付与です。user パラメーターはフィルターであり、認可ではありません。
- 会話のクリアでは、アクションノードが作成した永続的なファクトは削除されません。
- SDK またはサーバーが拒否した場合、ノードは失敗します。continue-on-fail の場合はエラー項目になります。
- アダプターはテーブルを作成せず、Loom の既存の消去パスを使用します。

<a id="source-entry-points"></a>

## ソースのエントリーポイント

- `integrations/n8n/nodes/LoomMemory/LoomChatHistory.ts`: メッセージの永続化。
- `integrations/n8n/nodes/LoomMemory/LoomMemory.node.ts`: フレームワークのメモリアダプター。
- `integrations/n8n/nodes/Loom/Loom.node.ts`: 永続メモリとドキュメントのアクション。
- `.github/workflows/n8n-publish.yml`: 検査と trusted publishing。

[フレームワークのセットアップ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)
