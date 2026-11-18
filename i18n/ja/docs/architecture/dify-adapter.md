<!-- i18n: source=docs/architecture/dify-adapter.md blob=34e577e662d3664872b0d8163d92fc018b884cd1 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md), which is authoritative.

<a id="dify-tools-adapter"></a>

# Dify ツールアダプター

Dify プラグインは Python SDK を通じて Loom を呼び出し、コンテンツをローカルに保存しません。3 つのネイティブツールは、それぞれ `memory.set`、`memory.search`、`knowledge.search` に対応します。オプションのスタンドアロンパッケージには、プラグイン SDK の 0.11 マイナーシリーズと Loom 1.x SDK が必要です。サーバーとコア SDK は Dify に依存しません。

<a id="invariants"></a>

## 不変条件

プロバイダーの認証情報は、エンドポイント、組織、名前空間、トークン、エージェントを束縛します。必須のフォームパラメーター User ID は、モデルの引数とは別にエンドユーザーを束縛します。リコールでは、そのユーザーのメモリを Loom のエージェントをまたいで読み取れます。ドキュメントは名前空間単位で共有されます。どちらの検索操作も `namespace_scope=this` に固定されます。相互に信頼できないアプリケーションは、名前空間を分け、トークンの付与範囲で制限します。Loom は、これらの呼び出しにも通常のアクター、来歴、競合処理、ローリングスキーマ、消去のルールを適用します。

HTTP の宛先は管理者が制御します。プラグインは URL の形式を検査し、埋め込まれた認証情報、クエリ、フラグメントを拒否します。プラグインランナーが到達できるホストは、デプロイ環境のネットワークポリシーで制限します。リクエストのタイムアウトは有限で、試行は 1 回です。ツールのエラーには、認証情報もリモートのレスポンスの詳細も含まれません。プラグインは、追加のデータベース、キャッシュ、モデルプロバイダー、テレメトリーの経路を作りません。

Dify は Python のスレッド処理を gevent グリーンレットでパッチします。各リクエストは、現在のハブのネイティブスレッドプール内で SDK クライアントの構築、呼び出し、クローズを行うため、重なり合うクライアントが同じネイティブスレッド上で asyncio ループを実行することはありません。認証情報はそのリクエスト内に閉じています。SDK 呼び出しの待機は最大 30 秒です。プールへの受け入れ、クライアントのセットアップ、クローズにはそれぞれ別の待機があり、呼び出し全体の期限はありません。

Dify は、ローカルの `.difypkg` からパッケージをインストールします。公式 CLI は、ソース、メタデータ、ランタイム要件、アイコン、ライセンス、README、プライバシーに関する声明をパッケージ化します。無視ファイルは、テスト、環境、認証情報、ビルド状態を除外します。CI は、偽の SDK HTTP トランスポートに対してネイティブツールを呼び出し、ループバック HTTP 上で重なり合うネイティブ呼び出しを確認し、ネイティブツールのスキーマを検証して、パッケージをビルドします。ライブテストは、明示的に指定されたプライベートな Loom の URL とトークンがある場合にのみ実行されます。プラットフォーム上での実行は、これらの SDK テストとは別に文書化されています。

<a id="source-entry-points"></a>

## ソースのエントリーポイント

- `integrations/dify/client.py`：スコープ付きの SDK 呼び出しとリクエストの失敗処理。
- `integrations/dify/provider/loom.py`：認証情報の検証。
- `integrations/dify/tools/`：ネイティブツールの実装とスキーマ。
- `.github/workflows/dify-plugin.yml`：テストとパッケージ化。
