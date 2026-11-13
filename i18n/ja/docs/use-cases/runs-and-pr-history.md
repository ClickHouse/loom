<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# エージェントの実行、トレース、PR 履歴

Loom は、コーディングエージェントが行った作業を、実行（run）と検索可能なセッション会話として記録し、コード履歴と結び付けます。run には目標と結果が含まれ、Claude Code プラグインがセッションのターン使用量を報告する場合（デフォルトで有効）は、モデルのコストも含まれます。履歴の対象は、マージ済みの pull request、レビューコメント、GitHub の issue で、tracker ソースを使う場合はインシデントと CI の実行も含まれます。会話の記録、PR 履歴、tracker は、それぞれ有効にするまでオフです。有効にすると、エージェントも人間も、以前に何を試したか、何が決定されたか、どの変更が回帰を引き起こしたかを尋ねられます。

<a id="what-you-get"></a>

## 得られるもの

- **Runs。** `run.begin` と `run.complete` は、タスクの目的、ステータス、エージェントが参照したメモリ、書き込んだ内容を記録します。 [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **会話とセッションブリーフ。** 記録されたセッションは検索でき、セッションブリーフにより次のセッションで作業を再開できます。記録は、名前空間の所有者が `conversation.set_logging(enabled=true)` で有効にするまでオフです。 [会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [長時間のエージェント実行をトレースして再開する](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **PR 履歴。** シンボル、パス、行の履歴、レビューブリーフ、PR 作成前の重複チェック、bisect の候補を提供します。詳細は[コードインデックスのページ](code-intelligence.md)を参照してください。
- **トラッカーレコード。** 重複 issue のチェック、issue と PR の紐付け、不安定なテスト（flaky test）を、チーム自身の ClickHouse からリアルタイムに読み取ります。`LOOM_TRACKER_ORG` と登録済みのトラッカーソースが必要です。 [Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **トレースとコスト。** Loom は OpenTelemetry を出力し、操作ごとに自身のモデル利用コストを記録します。Langfuse と ClickStack は任意の出力先です。
  [テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## 計測結果

公開されている ClickHouse の issue に対して、`tracker.duplicate_check` は、記録されている重複 104 件のうち 91 件（0.875）で古い issue を最上位にランク付けしました。また、重複としての紐付けが記録されていない 100 件の issue のうち 2 件を重複と判定しました。この結果は 1 つのトラッカーのものであり、対象の issue（スタックトレース、エラーコード、一貫して紐付けられた重複）は、多くのトラッカーの issue よりも扱いやすいものです。
[Duplicate check on public ClickHouse issues](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## 開始

[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)と、記録を有効にした後にセッションを取得するプラグインがインストールされます。[コードインデックス](code-intelligence.md)を使うには、リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
