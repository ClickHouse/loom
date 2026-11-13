<!-- i18n: source=docs/use-cases/code-intelligence.md blob=4f52e242dcb612150541843af58b15f37c31a265 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスを MCP 経由でコーディングエージェントに提供します。エージェントはファイル全体を読む代わりに、構造に関する質問を投げて、その答えとなる行を受け取れます。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と最新に保つ方法は[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)に、言語ごとに得られる内容は[言語別コードインデックス](code-intelligence-by-language.md)に記載しています。

<a id="what-you-get"></a>

## 得られるもの

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行を返します。各回答には、その根拠となるコミットとインデックスのティアも示されます。[コード操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と完全一致の文字列検索。** `code.search` は、ある仕組みがどこにあるかを見つけます。`code.grep` は、正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR 履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらには PR 履歴の有効化（`LOOM_CODE_PR_HISTORY_ENABLED`）が必要です。レビューコメントとオープンな PR には、GitHub トークンも必要です。それぞれに必要なものは[履歴、PR、リリース](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)で説明しています。
- **目に見える鮮度。** インデックスはブランチに対してマージ済みコミット分だけ遅れ、古い回答にはその旨が示されます。[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に含みます。Cursor などの MCP クライアントは `?toolset=coding` を使います。[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の最後に最大 3 つのファイルを挙げるよう指示しました。Loom を使った場合、概念的な質問 36 件（ペア実行 10 回）のうち 96.4% で正しいファイルを挙げました。Loom なしでは 91.4% でした。また、Loom を使った実行のコストは 21% 低くなりました（エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは除きます）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で、変更されたファイルを上位 10 件以内に順位付けしました（ファイルの特定であり、issue の解決ではありません）。比較対象は、インデックス済みの全ファイルに対する BM25 が 57%、`.py` ファイルのみに対する BM25 が 79% です。`.py` のみとの差は誤差の範囲内です。
[Loom ありとなし](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified ファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 開始する

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)で、リポジトリのインデックスを有効にします。ティアについては[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)で説明しています。

このページの一部の操作は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で実験的とされています。
