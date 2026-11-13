<!-- i18n: source=docs/use-cases/code-intelligence.md blob=4f52e242dcb612150541843af58b15f37c31a265 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスを MCP 経由でコーディングエージェントに提供します。エージェントはファイル全体を読み込む代わりに、構造に関する質問を投げて、その答えとなる行だけを受け取れます。利用するには、リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と最新の状態に保つ仕組みについては、[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。各言語で何が得られるかについては、[言語別のコードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)を参照してください。

<a id="what-you-get"></a>

## できること

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行を添えて回答します。各回答には、その根拠となるコミットとインデックスのティアも示されます。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と文字列の完全一致による検索。** `code.search` は、ある仕組みがどこにあるかを見つけます。`code.grep` は、正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらを使うには PR 履歴を有効にする必要があります（`LOOM_CODE_PR_HISTORY_ENABLED`）。レビューコメントとオープンな PR には GitHub トークンも必要です。それぞれの要件は [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) にあります。
- **目に見える鮮度。** インデックスはブランチに対してマージ済みコミット分だけ遅れ、古い回答にはその旨が示されます。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧表示します。Cursor やその他の MCP クライアントは `?toolset=coding` を使います。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の末尾へ最大 3 つのファイルを挙げさせました。Loom を使った場合、概念的な質問 36 問（10 回のペア実行）のうち 96.4% で正しいファイルを挙げました。Loom を使わない場合は 91.4% でした。また、Loom を使った実行はコストが 21% 低くなりました（エージェントのモデル利用料金で、Loom 自身のモデル呼び出しは除きます）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で変更されたファイルを上位 10 件以内にランク付けしました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス済みの全ファイルを対象にした BM25 は 57%、`.py` ファイルのみを対象にした BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified file localization](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を行うと、そのリポジトリのインデックスが有効になります。階層については[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で実験的としてマークされています。
