<!-- i18n: source=docs/reference/index.md blob=fb131cfc15bed6b11b4ba0dd63862e173de7bcf3 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md), which is authoritative.

<a id="reference"></a>

# リファレンス

リファレンスのページでは、何が存在し、各オプションが何を意味するかを説明します。設計の背景にある理由は、[コンセプト](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)と[アーキテクチャ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md)に記載しています。手順については、[ガイド](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)と[運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)のセクションを参照してください。

<a id="generated-from-the-code"></a>

## コードから生成されるページ

これらのページ、またはページ内の表は、それを定義しているソースから生成されます。各ジェネレーターには `--check` モードがあり、`make lint` がこれを実行するため、コードとずれたページは CI で失敗します。ソースを編集して再生成してください。生成された表を手で編集してはいけません。

| ページ                              | 掲載内容                                                                                                      | ソース                                                                               | ジェネレーター                                 |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------- |
| [設定](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) | すべての `LOOM_*` 設定について、デフォルト値と意味を領域別に掲載します。                                      | `server/settings/` 内のフィールド上のコメント                                        | `python scripts/codegen/settings_reference.py` |
| [オペレーション](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)       | すべてのオペレーションをグループ別に、ロール、種別、MCP の `all` 一覧、監査フラグとともに掲載します。あわせてレジストリの仕組みも説明します。 | `server.operations.registry.ALL_OPERATIONS`                                          | `python scripts/codegen/docs_reference.py`     |
| [ワーカー](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)             | すべてのバックグラウンドワーカーについて、スイッチ、デフォルト値、実行間隔を掲載します。あわせてスーパーバイザーがワーカーを実行する仕組みも説明します。 | `server/workers/registry.py`（`_WORKERS`）                                           | `python scripts/codegen/docs_reference.py`     |
| [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)                     | すべての `loom` コマンドとサブコマンドをフラグとともに掲載します。あわせて CLI の実行場所とよく使うコマンドも説明します。 | `loom` パーサーツリー（`tests/unit/snapshots/cli_tree.json`）                        | `python scripts/codegen/docs_reference.py`     |
| [エラー](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)               | サーバーが返すすべてのエラーコードを掲載します。あわせてエンベロープ、リトライのルール、主なコードの対処法も説明します。 | `server/core/errors.py` と `server/` 配下のすべての `"code"` リテラル                | `python scripts/codegen/docs_reference.py`     |
| [ステータス](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)               | 各オペレーショングループのうち安定版の数、各実験的オペレーション、および false でロードされるすべてのブール設定を掲載します。 | レジストリ内のオペレーションの `maturity` と `server/settings/catalog.py` のブールフィールド | `python scripts/codegen/docs_reference.py`     |
| [スキーマ](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)               | すべてのテーブルとマテリアライズドビューを、エンジン、キー、TTL、インデックスとともに掲載します。あわせてカラムの規約も説明します。 | `schemas/schema.sql`                                                                 | `python scripts/codegen/docs_reference.py`     |

`docs_reference.py` のページで生成されるのは、`<!-- BEGIN GENERATED: … -->` マーカーと `<!-- END GENERATED: … -->` マーカーの間の領域だけです。その周囲の文章は手書きです。SDK パッケージ内のオペレーション表（`sdk/python/loom/ops.py`、`sdk/typescript/src/generated/ops.ts`）も、`scripts/codegen/sdk_py_gen.py` と `sdk_ts_gen.py` によって生成されます。詳しくは[コード生成](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)を参照してください。

<a id="written-by-hand"></a>

## 手書きのページ

| ページ                                          | 内容                                                                                                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                     | すべてのルート（op、ディスカバリ、ヘルス、ストリーム、サインイン、OAuth、ダッシュボード、オペレーター）、ゲートの順序、ヘッダー、未知のフィールド、冪等性、ページネーション、レート制限。 |
| [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)                               | `/mcp` エンドポイント、認証と OAuth、セッションと ID、ツール一覧とツールセット、結果、エラー、アクションスキーマのエクスポート。                       |
| [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)                 | インストール、クライアントの構築、op の呼び出し、型付きの形状、セッションとツール、フレームワークアダプター、エラー、リトライ。                                                         |
| [TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)         | インストール、クライアントとオプション、型付き op、セッションとツール、ページネーション、Strands アダプター、エラー、リトライ。                                                     |
| [環境変数](https://github.com/ClickHouse/loom/blob/main/docs/reference/environment.md)     | 設定モデルの外で読み込まれる変数：プロバイダーキー、イメージ、compose ファイル、CLI、SDK、オペレータースクリプト。                                    |
| [SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)               | テナントのデータに対する読み取り専用の `sql_v1_*` ビュー：カラム、互換性ルール、非公開にする内容、鮮度、`loom sql-access`。                                |
| [エクストラクター契約](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md) | エクストラクターサービスが提供する HTTP 契約のバージョン 1：機能、抽出、エラーコード、アーティファクトのフィールドとルール。                                |

手書きのページには、ツリー内に存在するものだけを記載し、説明対象のソースへのリンクを付けています。

<a id="which-page-answers"></a>

## どのページが答えるか

- 「この op の名前は何か、誰が呼び出せるのか。」→ [操作](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)。
- 「この設定は何をするのか。」→ [設定](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)。
- 「このエラーコードは何を意味し、再試行すべきか。」→ [エラー](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)。
- 「この op は安定版か実験的か、この設定はデフォルトで有効か。」→ [ステータス](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)。
- 「どのヘッダー、どのルートか、ページングはどうするのか。」→ [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)。
- 「MCP クライアントにツールが表示されないのはなぜか。」→ [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)。
- 「このワーカーは動作しているか、有効にするにはどうするのか。」→ [ワーカー](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)。
- 「次回のデプロイでクライアントは壊れないか。」→ [不明なフィールドと削除されたフィールド](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)および[デプロイメントが提供する内容の確認](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)。
- 「このテーブルには何が入っているか。」→ [スキーマ](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)。
- 「`loom <command>` は何を受け取るか。」→ [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)。
