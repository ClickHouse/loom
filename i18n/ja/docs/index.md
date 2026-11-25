<!-- i18n: source=docs/index.md blob=173314858f057f518abf314eb98af3afff03c87b model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/index.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/index.md), which is authoritative.

<a id="clickhouse-loom-documentation"></a>

# ClickHouse Loom ドキュメント

Loom は、AI エージェント向けのメモリ、ドキュメント、コードインデックスのサーバーで、データは ClickHouse に保存されます。ライブのメモリとナレッジの状態には、オプションとして [Postgres](architecture/storage-authority.md#why-two-stores) も使えます。エージェントと人は、MCP、HTTP、Python および TypeScript の SDK、ダッシュボードから Loom を利用できます。このページでは、すべてのページを読む順に一覧にしています。

![ダッシュボードのライブラリ：返金ルールの検索に対して、引用付きの該当箇所が返された画面](https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/document-search.webp)

<video src="https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/clip-assistant-answer.webm" controls autoplay loop muted playsinline width="100%" aria-label="Ask AI answering a question about refunds, with its numbered sources checked against Loom"><a href="https://github.com/ClickHouse/loom/blob/main/docs/assets/screenshots/clip-assistant-answer.webm">Ask AI が確認済みの出典付きで回答する様子（クリップ）</a></video>

<a id="start-here"></a>

## はじめに

| ステップ                 | 読むページ                                                                                                                                                                                |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. 検討する              | [概要](getting-started/overview.md) → [Loom を評価する](getting-started/evaluate.md)                                                                                                      |
| 2. インストールする      | [クイックスタート](getting-started/quickstart.md) → [デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)                                                                                                      |
| 3. エージェントを接続する | [コーディングエージェントを接続する](getting-started/connect-a-coding-agent.md) → [インテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)                                                                      |
| 4. SDK と API            | [最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) → [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) / [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                                                            |
| 5. 名前空間とアクセス    | [ID と名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) → [トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)                                                                     |
| 6. コードインデックス    | [コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) → [リポジトリ](features/code-index/repositories.md) → [コードインデックスの運用](operations/code-index-operations.md)                        |
| 7. 運用する              | [モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md) · [トラブルシューティング](operations/troubleshooting.md) · [アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md) |

<a id="use-cases"></a>

## ユースケース

ニーズごとに 1 ページを用意しています。各ページでは、Loom がそのニーズに対して何を行うか、どこから始めればよいかを説明します。
[エージェントメモリ](use-cases/agent-memory.md) ·
[ドキュメント検索と RAG](use-cases/document-search.md) ·
[コーディングエージェント向けコードインデックス](use-cases/code-intelligence.md) ·
[言語別コードインデックス](use-cases/code-intelligence-by-language.md) ·
[コードに紐づくメモリ](use-cases/memory-linked-to-code.md) ·
[コーディングエージェントのモデルコスト](use-cases/coding-agent-cost.md) ·
[複数エージェントでの共有コンテキスト](use-cases/multi-agent.md) ·
[自律エージェント：想起、監査、消去](use-cases/autonomous-agents.md) ·
[実行、トレース、PR 履歴](use-cases/runs-and-pr-history.md) ·
[ClickHouse でのセルフホスト](use-cases/self-hosted-clickhouse.md) ·
[すべてのユースケース](use-cases/index.md)。

<a id="1-getting-started"></a>

## 1. はじめに

- [概要](getting-started/overview.md)：Loom とは何か、どのような場面に適しているか、前提条件。
- [含まれるもの](getting-started/whats-included.md)：Loom の主な機能と、それぞれのページ。
- [クイックスタート](getting-started/quickstart.md)：ローカルスタックを最初から最後まで構築する手順。
- [モデルと API キーの設定](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)：エージェントのサブスクリプション、サーバー LLM、埋め込み、単一プロバイダーの構成。
- [Loom を評価する](getting-started/evaluate.md)：スモークテスト、ワークロード評価、再現可能な公開ベンチマーク。
- [コーディングエージェントを接続する](getting-started/connect-a-coding-agent.md)：プラグインまたは MCP のエントリ。
- [コーディングエージェントで Loom をセットアップする](getting-started/agent-setup.md)：固定バージョンのリリースをインストールして動作を確認するエージェント向けで、決まった形式のレポートを出力します。
- [最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：SDK のチュートリアル。

<a id="2-install-and-run-the-server"></a>

## 2. サーバーのインストールと実行

- [デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)：構成、環境、プロバイダー、ID モード、プロキシ、イメージ、本番環境チェックリスト。
- [シングルサインオン](operations/sso.md)：ダッシュボード、`loom login`、MCP クライアント向けの OIDC ID プロバイダー。
- [ダッシュボードを使わずに Loom を使う](guides/without-the-dashboard.md)：ダッシュボードの各ビューに対応する `loom login`、`loom call`、SDK の呼び出し。
- [アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [サポート対象バージョン](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md) ·
  [バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md) ·
  [モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md) · [メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md) ·
  [ランタイム設定](operations/runtime-settings.md) ·
  [トラブルシューティング](operations/troubleshooting.md) ·
  [コードインデックスの運用](operations/code-index-operations.md) ·
  [N 個のレプリカの運用](operations/replicas.md)：1 つの URL の背後にある同一の Loom ·
  [Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)：Postgres のメモリオーソリティ ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)：EKS と kind のオーバーレイを備えた Helm チャート ·
  [ClickHouse Cloud と Managed Postgres](operations/clickhouse-cloud.md)：両方のマネージドサービスに対応するチャート ·
  [名前空間を別の Loom にコピーする](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) ·
  [使用状況をデータウェアハウスにエクスポートする](operations/analytics-export.md)

<a id="3-connect-agents"></a>

## 3. エージェントを接続する

[連携](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md) ·
[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ·
[エージェント向け指示](features/integrations/agent-instructions.md) ·
[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) ·
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) · [Cursor](features/integrations/cursor.md) ·
[VS Code](features/integrations/vscode.md) ·
[フレームワーク](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) ·
[Grep シム](features/integrations/grep-shim.md) ·
[Langfuse](features/integrations/langfuse.md) ·
[ClickStack](features/integrations/clickstack.md) ·
[OKF](features/integrations/okf.md)

<a id="4-sdk-and-api"></a>

## 4. SDK と API

[リファレンス索引](reference/index.md) · [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) ·
[MCP](reference/mcp.md) · [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) ·
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) · [環境変数](reference/environment.md) ·
[API オペレーション](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md) · [設定](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) ·
[ワーカー](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md) · [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md) · [エラー](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md) ·
[ステータス](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) · [スキーマ](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md) ·
[エクストラクタ契約](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md)。オペレーションとフィールドはリリース間で変更されます。サーバーが読み取らなくなったフィールドをクライアントがどのように認識するかについては、[未知のフィールドと削除されたフィールド](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)を参照してください。

<a id="5-namespaces-access-and-the-memory-model"></a>

## 5. 名前空間、アクセス、メモリモデル

- [アイデンティティと名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)：タプル、ロール、グラント、読み取りスコープ。
- [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)：行、書き込みの判定方法、時間、履歴、来歴。
- [検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)：レッグ、フュージョン、アブステイン、キャリブレーション。

<a id="guides"></a>

## ガイド

[エージェント連携](guides/agent-integration.md) ·
[ヘッドレスボット](guides/headless-bots.md) ·
[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) ·
[長時間実行のトレースと再開](guides/trace-and-resume.md) ·
[名前空間の最適化](guides/optimize-namespace.md) ·
[調査エージェントのメモリ](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md) ·
[抽出パイプライン](guides/extraction-pipeline.md) ·
[パフォーマンスチューニング](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md) · [デバッグ](guides/debugging.md) ·
[Postgres から Loom にクエリする](guides/query-from-postgres.md) ·
[Loom で教える](guides/teaching.md)

<a id="features"></a>

## 機能

- プレーン：[名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) · [ナレッジ](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md) ·
  [エンティティグラフ](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md) ·
  [会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) · [タスクと意思決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) ·
  [ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)
- メモリ：[書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md) ·
  [検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) ·
  [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) ·
  [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md) ·
  [保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) ·
  [手順と学習内容](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md) ·
  [ワーキングメモリとプロファイル](features/memory/working-memory-and-profiles.md)
- オブザーバビリティ：[監査](features/observability/audit.md) ·
  [コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md) ·
  [テレメトリ](features/observability/telemetry.md)
- トラッカー：[概要](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) ·
  [レコード契約](features/tracker/record-contract.md)

<a id="6-code-index"></a>

## 6. コードインデックス

[概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) ·
[リポジトリ](features/code-index/repositories.md) ·
[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md) ·
[履歴と PR](features/code-index/history-and-prs.md) ·
[オペレーション（API 操作）](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md) ·
[コードインデックス運用ランブック](operations/code-index-operations.md)

<a id="architecture"></a>

## アーキテクチャ

[システム概要](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) ·
[サーバーレイアウト](architecture/server-layout.md) ·
[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md) ·
[メモリアーキテクチャ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) ·
[ストレージオーソリティ（正のデータの保存先）](architecture/storage-authority.md) ·
[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md) ·
[ストレージレイアウト](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md) ·
[コードインデックスのアーキテクチャ](architecture/code-index-architecture.md) ·
[ドキュメント抽出](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-extraction.md) ·
[ドキュメントソース](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md) ·
[LLM と埋め込み](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md) ·
[セキュリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md) ·
[アシスタントの設計](https://github.com/ClickHouse/loom/blob/main/docs/architecture/assistant.md) ·
[障害と復旧](https://github.com/ClickHouse/loom/blob/main/docs/architecture/failure-and-recovery.md) ·
[ローリングアップグレード](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)

<a id="benchmarks"></a>

## ベンチマーク

[概要](benchmarks/overview.md) · [カバレッジ](benchmarks/coverage.md) · [方法論](benchmarks/methodology.md) · [実行方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) · [結果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) · [ケースの追加](benchmarks/adding-a-case.md) · [スキル評価](benchmarks/skill-eval.md)

<a id="development"></a>

## 開発

コントリビューター向け：[リポジトリガイド](development/repository-guide.md) ·
[ローカル開発](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md) ·
[テスト](development/testing.md) · [コード生成](development/code-generation.md) ·
[マイグレーション](development/migrations.md) ·
[リリースプロセス](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md) ·
[ドキュメントスタイル](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md) ·
[キュレーションループの設計](https://github.com/ClickHouse/loom/blob/main/docs/development/curation-loop-design.md) ·
[Loom の拡張](development/extending-loom.md) ·
[オペレーションの追加](development/adding-an-operation.md) ·
[ワーカーの追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-worker.md) ·
[テーブルの追加](development/adding-a-table.md) ·
[言語サポートの追加](development/adding-language-support.md) ·
[ダッシュボードの設計](https://github.com/ClickHouse/loom/blob/main/docs/development/dashboard-design.md)。
プルリクエストのワークフローについては [CONTRIBUTING.md](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) を参照してください。

<a id="also"></a>

## 関連ページ

[用語集](glossary.md)：Loom の用語を一か所にまとめて定義しています。[よくある質問](faq.md)：最初に出てくる質問に、それぞれ一段落で答えます。[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)：エージェント向けに生成されたドキュメントの索引です。[`llms-full.txt`](https://loom.clickhouse.com/docs/llms-full.txt) には、Loom とは何か、どう始めるかを説明するページが 1 つのファイルにまとめられています。
