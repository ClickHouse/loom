<!-- i18n: source=docs/getting-started/whats-included.md blob=62311ea2165c49b6c91511404f1308be7524c42a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/whats-included.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/whats-included.md), which is authoritative.

<a id="whats-included"></a>

# 含まれる機能

このページでは、Apache-2.0 リポジトリからセルフホストできる Loom の主な機能を一覧にしています。分野ごとにセクションを分け、各行から、その機能を扱うページへリンクしています。残りのドキュメントは[ドキュメント索引](https://github.com/ClickHouse/loom/blob/main/docs/index.md)にまとめています。いくつかの機能は有効化が必要です。方法はリンク先のページに記載しています。また、デフォルトで false になるブール値のスイッチは[ステータス](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)に一覧があります。エンベッダーは自由に選べます。同梱のローカルモデル、ホスト型プロバイダー、自分で運用する OpenAI 互換のエンドポイントのいずれも使えます。抽出、要約、引用付きの回答には実際の LLM が必要です（[モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)）。

<a id="memory"></a>

## メモリ

| 機能                  | 内容                                                                                                                           | ページ                                                                             |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| 共有長期メモリ     | 名前空間内のキーごとに1つの主張を保持し、書き込まれる前にすでにあるファクトと照合して判定する                                               | [メモリへの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)                                  |
| 型付きレコード               | 識別性を持つデータのための、正確なフィールドを持つ行。テキストを解析せずに述語による問い合わせに答える                                  | [型付きレコード](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records)                     |
| 検索と質問              | ランク付けされたヒット、またはメモリのキーを引用した回答を返す。メモリに裏付けがない場合は、どちらも回答を控える                                | [検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)                           |
| 競合と拒否       | 近いが異なる値は競合として検出され、名前空間の所有者がどのファクトを採用するかを決める。登録されたパターンに一致する書き込みは拒否される | [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)             |
| 履歴と来歴      | すべての書き込みは新しいバージョンとなり、誰が書き込んだか、どのようにメモリに入ったかが記録される                                                              | [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)           |
| トランスクリプトからの抽出 | トランスクリプトのうち何が永続的な内容かをサーバーが判断する                                                                       | [抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)                                   |
| インポート                      | 別のメモリシステムがすでに蒸留したファクトを、通常の書き込みと同じチェックを経て書き込む                                | [インポート](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#importing-from-another-memory-system)  |
| 変更 webhook             | メモリが書き込まれた、改訂された、削除された、復元された、墨消しされた、または消去されたときの署名付きイベント。名前空間の管理者が登録した URL に送信される   | [変更 webhook](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#change-webhooks)                 |
| ワーキングメモリとプロファイル | エージェントに毎回渡される、名前付きの小さなブロックとプロファイルのテキスト                                                                       | [ワーキングメモリとプロファイル](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md) |
| 手順と学習内容    | 報告された実行、振り返り、候補ファクトのレビューキューから学習した手順                                                                  | [手順と学習内容](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)       |
| コードに紐づくメモリ     | コードに関するメモリは、そこで名指しされた定義が移動したり変更されたりしても、それに追従する                                                                    | [コードに紐づくメモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)         |
| エンティティグラフ                | メモリの行と並んで保持されるエンティティとその関係                                                                            | [エンティティグラフ](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)                                      |

<a id="documents-code-and-work-records"></a>

## ドキュメント、コード、作業記録

| 機能                          | 概要                                                                                                                                                                    | ページ                                                                                                                                                          |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ドキュメント検索              | ドキュメントをセクションに分割して埋め込み、パッセージ単位で検索・引用します。何も見つからなかった検索はナレッジギャップとしてまとめられます                            | [ナレッジドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md) · [ナレッジギャップ](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#knowledge-gaps)                                                     |
| ファイルとドキュメントソース  | PDF、DOCX、PPTX、XLSX、CSV などを抽出サービスで解析します。登録したソースは名前空間に同期されます                                                                        | [ファイルの取り込み](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files) · [ドキュメントソース](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)                                     |
| 統合検索                      | メモリ、プロファイル、ドキュメント、過去の会話を 1 回の呼び出しで検索し、タスク向けのコンテキストパックを返します                                                        | [統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)                                                                                                                 |
| AI に質問                     | 名前空間が保持する内容について、すべての主張に引用を付けて回答するダッシュボード内のアシスタントです。モデルの回答は、Loom の隣にデプロイするチャットサーバー上で実行されます | [ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md#ask-ai) · [チャットサーバーのセットアップ](https://github.com/ClickHouse/loom/blob/main/docs/operations/assistant.md)                                |
| コードインデックス            | git リポジトリに対する定義、呼び出し元、影響範囲、意味による検索を提供します                                                                                            | [コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)                                                                                                                |
| PR とリリースの履歴           | マージ済みプルリクエストを、現在のシンボル、呼び出し元、テストと結び付けます                                                                                            | [履歴、PR、リリース](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)                                                                                          |
| トラッカー                    | Issue、プルリクエスト、インシデント、CI 実行を、自分の ClickHouse からライブで読み取ります                                                                              | [トラッカープレーン](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)                                                                                                                |
| 会話とセッション              | 管理者が名前空間のログ記録を有効にすると記録される検索可能な逐語のトランスクリプトと、エージェントが再開に使うセッションブリーフです                                      | [会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) · [記録](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md#recording-is-the-owners-choice) |
| 実行                          | タスク実行ごとに 1 件のレコードを作成し、そのタスクが依拠したメモリも記録します                                                                                         | [実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)                                                                                                                                     |
| タスク、決定、ポリシー        | エージェント間の協調のための、タスクインボックス、決定記録、ポリシーです                                                                                                | [タスク、決定、ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)                                                                                             |

<a id="access-governance-and-operations"></a>

## アクセス、ガバナンス、運用

| 機能                           | 内容                                                                                                                          | ページ                                                                                                                                                   |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 名前空間と共有                 | 組織内での分離と共有の単位。すべての読み取りは 1 つの名前空間にスコープされる                                                 | [名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)                                                                                          |
| トークンとロール               | 名前空間ごとのトークン、グラント、ロール                                                                                      | [トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)                                                                                                      |
| シングルサインオンと SCIM      | ダッシュボード、CLI、MCP クライアント向けの OIDC サインインと、SCIM によるデプロビジョニング                                  | [シングルサインオン](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)                                                                                                                   |
| 監査ログ                       | 監査対象の操作またはサーバーイベントごとに 1 行。自由記述の値はハッシュとして保存され、検証済みのチェックポイントチェーンに封じられる | [監査ログ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)                                                                                                          |
| 保持と消去                     | 有効期限、設定された保持期間、Loom が保持するあらゆる場所でのスコープの消去                                                   | [保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)                                                                                     |
| 機密値                         | `high` 機密度のメモリは埋め込まれず、キーでのみ読み取れ、その読み取りは監査される。検出は設定可能                             | [メモリタイプ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memory-types)                                                                                                       |
| モデル呼び出しの制御           | 組織の管理者は組織のデータに対するすべてのモデル呼び出しを無効にでき、デプロイメントは呼び出しを推論ゲートウェイ経由にできる  | [モデル呼び出しの無効化](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#switching-an-orgs-model-calls-off) · [推論ゲートウェイ](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#through-an-inference-gateway) |
| コストと使用量                 | すべての埋め込みと LLM の費用を、それを消費した操作に紐づけて記録                                                             | [コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)                                                                                            |
| テレメトリ                     | 操作ごとの台帳、OpenTelemetry のトレース、ログ、メトリクス、クエリのアトリビューション                                        | [テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)                                                                                                      |
| ダッシュボード                 | 競合、提案、タスク、グラントリクエスト、ポリシーを人が判断する Web アプリ                                                     | [ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)                                                                                                                    |
| SQL アクセス                   | テナントのメモリ、履歴、会話、リクエスト台帳に対する読み取り専用の ClickHouse ビュー。Postgres からも読み取れる               | [SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) · [Postgres からのクエリ](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)                                                     |
| レプリカと Kubernetes          | 1 つの URL の背後にある同一のレプリカと、Helm チャート                                                                        | [N 個のレプリカの実行](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md) · [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)                                                              |
| Postgres メモリオーソリティ    | オプション：稼働中のメモリとドキュメントのヘッドを Postgres に置く。初回起動時に選択する                                      | [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)                                                                                                                    |
| バックアップとリカバリ         | ClickHouse のバックアップ。使用している場合はドキュメントバケットと Postgres も対象                                           | [バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)                                                                                              |
| アップグレード                 | イメージを置き換えると、マイグレーションが起動時に適用される。新規インストールも同じ経路をたどる                              | [アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)                                                                                                                    |
| 名前空間のコピーと OKF バンドル | 名前空間をスケジュールに従って別の Loom へコピーするか、OKF バンドルとしてエクスポートおよびインポートする                   | [名前空間のコピー](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) · [OKF バンドル](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md)                                                        |
| 使用量のエクスポート           | UTC 1 日分の使用量と名前空間のスナップショットを、データウェアハウス向けに Parquet として S3 に書き出す                       | [分析エクスポート](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)                                                                                                    |

<a id="ways-to-connect"></a>

## 接続方法

任意の MCP クライアント向けの MCP、Claude Code と Codex 向けのプラグイン、フレームワークアダプター、HTTP API、Python SDK と TypeScript SDK、そして `loom` CLI があります：
[インテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md) ·
[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ·
[フレームワーク](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) ·
[HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) · [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) ·
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) · [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)。
