<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# エージェントの実行、トレース、PR 履歴

Loom は、コーディングエージェントが行う作業を、実行（run）と検索可能なセッション会話として記録し、コード履歴と結び付けます。実行にはゴールと結果が含まれ、Claude Code プラグインがセッションのターン使用量を報告する場合（デフォルトで有効）にはモデルコストも含まれます。履歴の対象は、マージ済みのプルリクエスト、レビューコメント、GitHub の issue、そしてトラッカーソースがある場合はインシデントと CI の実行です。会話の記録、PR 履歴、トラッカーは、それぞれ有効にするまでオフです。有効にすると、エージェントや人は、過去に何を試したか、何を決定したか、どの変更がリグレッションを引き起こしたかを問い合わせられます。

<a id="what-you-get"></a>

## 提供される機能

- **実行（Runs）。** `run.begin` と `run.complete` は、タスクのゴール、ステータス、エージェントが利用したメモリ、書き込んだ内容を記録します。[Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **会話とセッションブリーフ。** 記録されたセッションは検索可能で、セッションブリーフにより次のセッションで作業を再開できます。記録は、名前空間のオーナーが `conversation.set_logging(enabled=true)` で有効にするまでオフです。[Conversations and sessions](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Trace and resume long agent runs](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **PR 履歴。** シンボル、パス、行の履歴、レビューブリーフ、PR 作成前の重複チェック、bisect の候補を提供します。詳細は[コードインデックスのページ](code-intelligence.md)を参照してください。
- **トラッカーレコード。** issue の重複チェック、issue と PR のリンク、不安定なテストを、チーム自身の ClickHouse からライブで読み取ります。`LOOM_TRACKER_ORG` と登録済みのトラッカーソースが必要です。[Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **トレースとコスト。** Loom は OpenTelemetry を出力し、操作ごとの自身のモデル支出を記録します。Langfuse と ClickStack はオプションのシンクです。[Telemetry](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Cost and usage](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## 計測結果

公開されている ClickHouse の issue では、`tracker.duplicate_check` は、記録された 104 件の重複のうち 91 件（0.875）で古い方の issue を先頭にランク付けし、重複の記録がない 100 件の issue のうち 2 件を重複と判定しました。この結果は 1 つのトラッカーのものであり、その issue（スタックトレース、エラーコード、一貫してリンクされた重複）は、多くのトラッカーよりも扱いやすいものです。[Duplicate check on public ClickHouse issues](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## 開始する

[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) では、記録を有効にした後にセッションを取得するプラグインをインストールします。[コードインデックス](code-intelligence.md)には、リポジトリの追加と `LOOM_CODE_INGEST_ENABLED=true` が必要です。

このページの一部の操作は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
