<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# エージェントの実行、トレース、PR 履歴

Loom は、コーディングエージェントが行った作業を実行（run）と検索可能なセッション会話として記録し、コード履歴に結び付けます。実行にはゴールと結果が記録され、Claude Code プラグインがセッションのターン使用量を報告する場合（デフォルトで有効）はモデルのコストも記録されます。履歴の対象は、マージ済みのプルリクエスト、レビューコメント、GitHub の issue で、トラッカーソースがあればインシデントと CI の実行も含まれます。会話の記録、PR 履歴、トラッカーはそれぞれ、有効にするまでオフです。有効にすると、エージェントや人間は、以前に何を試したか、何を決めたか、どの変更がリグレッションを引き起こしたかを問い合わせられます。

<a id="what-you-get"></a>

## 提供される機能

- **実行（Runs）。** `run.begin` と `run.complete` は、タスクのゴール、ステータス、エージェントが頼りにしたメモリ、書き込んだ内容を記録します。[実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **会話とセッションブリーフ。** 記録されたセッションは検索でき、セッションブリーフにより次のセッションで作業を再開できます。記録は、名前空間の所有者が `conversation.set_logging(enabled=true)` で有効にするまでオフです。[会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ・ [長時間のエージェント実行をトレースして再開する](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **PR 履歴。** シンボル、パス、行の履歴、レビューブリーフ、PR 作成前の重複チェック、bisect 候補を提供します。[コードインデックスのページ](code-intelligence.md)も参照してください。
- **トラッカーレコード。** issue の重複チェック、issue と PR のリンク、不安定なテストを、チーム自身の ClickHouse からリアルタイムに読み取ります。`LOOM_TRACKER_ORG` と登録済みのトラッカーソースが必要です。[トラッカープレーン](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **トレースとコスト。** Loom は OpenTelemetry を出力し、オペレーションごとに自身のモデル利用コストを記録します。Langfuse と ClickStack はオプションの送信先です。[テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ・ [コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## 計測結果

公開されている ClickHouse の issue では、`tracker.duplicate_check` は、記録された 104 件の重複のうち 91 件（0.875）で古い issue を先頭にランク付けし、重複リンクが記録されていない 100 件の issue のうち 2 件を重複と判定しました。この結果は 1 つのトラッカーのものであり、その issue（スタックトレース、エラーコード、一貫してリンクされた重複）は、多くのトラッカーのものより扱いやすい内容です。[公開 ClickHouse issue での重複チェック](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## はじめに

[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)と、記録が有効になった後にセッションを取得するプラグインがインストールされます。[コードインデックス](code-intelligence.md)には、リポジトリの追加と `LOOM_CODE_INGEST_ENABLED=true` が必要です。

このページの一部のオペレーションは、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で実験的とマークされています。
