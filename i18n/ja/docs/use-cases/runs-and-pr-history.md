<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Agent runs, traces and PR history

Loom は、コーディングエージェントが行った作業を run と検索可能なセッション会話として記録し、コードの履歴と結びつけます。run にはゴールと結果が記録されます。Claude Code プラグインがセッションのターン使用量を報告する場合 (デフォルトで有効) は、モデルのコストも記録されます。履歴には、マージ済みの pull request、レビューコメント、GitHub の issue が含まれ、トラッカーソースを設定すればインシデントと CI の実行も含まれます。会話の記録、PR 履歴、トラッカーはいずれも、有効にするまでオフです。有効にすると、エージェントや人間は、過去に何を試したか、何を決定したか、どの変更がリグレッションを引き起こしたかを問い合わせられます。

<a id="what-you-get"></a>

## 得られるもの

- **Runs。** `run.begin` と `run.complete` は、タスクの目標、ステータス、エージェントが参照したメモリ、エージェントが書き込んだ内容を記録します。 [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **会話とセッションブリーフ。** 記録されたセッションは検索でき、セッションブリーフにより次のセッションで作業を再開できます。記録は、namespace のオーナーが `conversation.set_logging(enabled=true)` で有効にするまでオフです。 [会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [長時間のエージェント実行をトレースして再開する](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **PR 履歴。** シンボル、パス、行の履歴、レビューブリーフ、PR 作成前の重複チェック、bisect の候補を提供します。詳細は[コードインデックスのページ](code-intelligence.md)を参照してください。
- **トラッカーのレコード。** 重複 issue のチェック、issue と PR のリンク、不安定なテスト（flaky test）を、チーム自身の ClickHouse からリアルタイムに読み取ります。`LOOM_TRACKER_ORG` と登録済みのトラッカーソースが必要です。 [トラッカープレーン](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **トレースとコスト。** Loom は OpenTelemetry を出力し、操作ごとの自身のモデル利用コストを記録します。Langfuse と ClickStack は任意の出力先です。
  [テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## 計測結果

公開されている ClickHouse の issue に対して、`tracker.duplicate_check` は、重複として記録されている 104 件のうち 91 件(0.875)で古い issue を先頭に順位付けしました。また、重複リンクが記録されていない 100 件の issue のうち 2 件を重複と判定しました。この結果は 1 つのトラッカーのものであり、その issue(スタックトレース、エラーコード、一貫してリンクされた重複)は、多くのトラッカーのものよりも扱いやすい内容です。
[公開 ClickHouse issue での重複チェック](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## 開始

[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)と、記録が有効になった後にセッションをキャプチャするプラグインがインストールされます。[コードインデックス](code-intelligence.md)を使うには、リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と表示されています。
